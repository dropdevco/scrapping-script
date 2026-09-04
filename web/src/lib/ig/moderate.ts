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
