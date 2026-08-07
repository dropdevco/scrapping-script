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
    .update({ status: "approved" })
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
    .update({ status: "approved", scheduled_for: new Date().toISOString() })
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
