import { triggerImmediatePublish } from "@/lib/ig/githubDispatch";
import { supabaseAdmin } from "@/lib/supabase/admin";

/* Shared by the session-authed admin actions, the token-authed review page,
   and the Telegram webhook — one CAS implementation instead of three copies
   that can drift. Callers are responsible for their own auth gate; this only
   does the database transition. */

type ModerateResult = { ok: true } | { ok: false; message: string };

export async function approveIgPostRow(id: string): Promise<ModerateResult> {
  const { data, error } = await supabaseAdmin()
    .from("ig_posts")
    .update({ status: "approved", approved_by: "human" })
    .eq("id", id)
    .eq("status", "draft")
    .select("id");
  if (error) return { ok: false, message: error.message };
  if (!data?.length) return { ok: false, message: "That post is no longer a draft." };
  return { ok: true };
}

export async function rejectIgPostRow(id: string): Promise<ModerateResult> {
  const { data, error } = await supabaseAdmin()
    .from("ig_posts")
    .update({ status: "rejected" })
    .eq("id", id)
    .eq("status", "draft")
    .select("id");
  if (error) return { ok: false, message: error.message };
  if (!data?.length) return { ok: false, message: "That post is no longer a draft." };
  return { ok: true };
}

/* Same CAS as approveIgPostRow, but also pulls scheduled_for to right now —
   the publish sweep only ships rows whose scheduled_for has arrived, so
   without this an "approve" and a "publish now" would behave identically. */
export async function publishIgPostNowRow(id: string): Promise<ModerateResult> {
  const { data, error } = await supabaseAdmin()
    .from("ig_posts")
    .update({ status: "approved", approved_by: "human", scheduled_for: new Date().toISOString() })
    .eq("id", id)
    .eq("status", "draft")
    .select("id");
  if (error) return { ok: false, message: error.message };
  if (!data?.length) return { ok: false, message: "That post is no longer a draft." };
  await triggerImmediatePublish();
  return { ok: true };
}

/* Reschedule only while still a draft — once approved, the publish sweep may
   already be about to claim it, and editing the time out from under a
   claim would just be confusing rather than useful. */
export async function rescheduleIgPostRow(id: string, isoTime: string): Promise<ModerateResult> {
  const { data, error } = await supabaseAdmin()
    .from("ig_posts")
    .update({ scheduled_for: isoTime })
    .eq("id", id)
    .eq("status", "draft")
    .select("id");
  if (error) return { ok: false, message: error.message };
  if (!data?.length) return { ok: false, message: "That post is no longer a draft." };
  return { ok: true };
}

/* Cancel accepts 'approved' as well as 'draft', unlike rejectIgPostRow.

   With opt-out posting the cancel window legitimately extends past
   auto-approval: the 17:00 sweep flips the row by itself, and if Cancel still
   required a draft, the button in the Telegram message would start answering
   "That post is no longer a draft" at exactly the moment someone most wants to
   stop it. The window closes at 'publishing', where claim_ig_post has taken
   the row and Meta is already involved. */
export async function cancelIgPostRow(id: string): Promise<ModerateResult> {
  const { data, error } = await supabaseAdmin()
    .from("ig_posts")
    .update({ status: "rejected" })
    .eq("id", id)
    .in("status", ["draft", "approved"])
    .select("id");
  if (error) return { ok: false, message: error.message };
  if (!data?.length) return { ok: false, message: "That post has already gone out." };
  return { ok: true };
}

/* Postpone MUST move auto_approve_at as well as scheduled_for.

   Moving only scheduled_for would leave tonight's deadline armed: the sweep
   would auto-approve the post at 17:00 today and publish it tomorrow with
   today's staleness guard expiring it in between. This is the sharpest edge in
   the opt-out design, which is why it is one function rather than a documented
   two-step. */
export async function postponeIgPostRow(id: string, isoTime: string): Promise<ModerateResult> {
  const { data, error } = await supabaseAdmin()
    .from("ig_posts")
    .update({ scheduled_for: isoTime, auto_approve_at: isoTime })
    .eq("id", id)
    .in("status", ["draft", "approved"])
    .select("id");
  if (error) return { ok: false, message: error.message };
  if (!data?.length) return { ok: false, message: "That post has already gone out." };
  return { ok: true };
}

/* Instagram's own cap. Rejected before the write so the user gets a useful
   message from the bot rather than a Graph API error hours later at publish. */
export const MAX_CAPTION_CHARS = 2200;

/* Editing the caption needs no re-render: the caption is never drawn onto a
   slide (render.py never sees it) — _publish_one reads this column at publish
   time. That is what makes caption editing a plain UPDATE while dropping an
   event needs a full rebuild. */
export async function updateIgPostCaptionRow(
  id: string,
  caption: string,
): Promise<ModerateResult> {
  const text = caption.trim();
  if (!text) return { ok: false, message: "Caption can't be empty." };
  if (text.length > MAX_CAPTION_CHARS) {
    return {
      ok: false,
      message: `Caption is ${text.length} chars; Instagram's limit is ${MAX_CAPTION_CHARS}.`,
    };
  }
  const { data, error } = await supabaseAdmin()
    .from("ig_posts")
    .update({ caption: text })
    .eq("id", id)
    .in("status", ["draft", "approved"])
    .select("id");
  if (error) return { ok: false, message: error.message };
  if (!data?.length) return { ok: false, message: "That post has already gone out." };
  return { ok: true };
}

/* An edit request the webhook cannot carry out itself.

   Dropping an event or swapping a photo means re-rendering the carousel with
   Pillow, which this serverless handler cannot do. So a tap records the
   INTENT here and `python -m scraper.social apply-edits` performs it — either
   from the immediate dispatch, or from the next publish sweep if that
   dispatch failed. Until it is applied, the row blocks auto-approval, so a
   post can never ship still containing the event someone asked to remove. */
export async function requestIgEdit(
  postId: string,
  op: "drop_event" | "swap_photo",
  payload: Record<string, unknown>,
): Promise<ModerateResult> {
  // Only a draft is editable: past that the publisher may already have
  // claimed the row, and re-rendering slides out from under Meta is worse
  // than refusing.
  const { data: post, error: readErr } = await supabaseAdmin()
    .from("ig_posts")
    .select("id, status, event_ids")
    .eq("id", postId)
    .single();
  if (readErr) return { ok: false, message: readErr.message };
  if (post?.status !== "draft") return { ok: false, message: "That post has already gone out." };

  if (op === "drop_event") {
    // Checked here as well as in apply-edits so the user gets an instant
    // answer rather than a rebuild that silently refuses ten minutes later.
    const remaining = ((post.event_ids as string[] | null) ?? []).length - 1;
    const min = Number(process.env.IG_MIN_SLIDES || 4);
    if (remaining < min) {
      return {
        ok: false,
        message: `Can't drop — that leaves ${remaining} slides (minimum ${min}). Cancel the post instead?`,
      };
    }
  }

  const { error } = await supabaseAdmin()
    .from("ig_post_edits")
    .insert({ post_id: postId, op, payload });
  if (error) return { ok: false, message: error.message };
  return { ok: true };
}

/* Titles for the "which one?" keyboard. ig_posts stores only event_ids, and
   querying here avoids both a denormalised column and titles going stale. */
export async function igPostEventChoices(
  postId: string,
): Promise<{ id: string; title: string; index: number }[]> {
  const { data: post } = await supabaseAdmin()
    .from("ig_posts")
    .select("event_ids")
    .eq("id", postId)
    .single();
  const ids = ((post?.event_ids as string[] | null) ?? []).filter(Boolean);
  if (!ids.length) return [];
  const { data: events } = await supabaseAdmin()
    .from("events")
    .select("id, title")
    .in("id", ids);
  const byId = new Map((events ?? []).map((e) => [e.id as string, e.title as string]));
  // Indexed by position in event_ids, which is the order apply-edits uses.
  return ids.map((id, index) => ({ id, index, title: byId.get(id) ?? "(untitled)" }));
}
