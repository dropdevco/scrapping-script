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
