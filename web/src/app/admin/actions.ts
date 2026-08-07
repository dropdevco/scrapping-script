"use server";

import { revalidatePath } from "next/cache";
import { isAdminEmail } from "@/lib/admin";
import {
  approveIgPostRow,
  publishIgPostNowRow,
  rejectIgPostRow,
  rescheduleIgPostRow,
} from "@/lib/ig/moderate";
import { supabaseAdmin } from "@/lib/supabase/admin";
import { supabaseServer } from "@/lib/supabase/server";

/* Server Actions are directly callable (not just reachable through the page's own
   UI), so each one re-checks admin auth itself — never rely solely on the page
   component's gate. Exported so sibling admin routes reuse this exact gate
   rather than reimplementing (and drifting from) the auth check. */
export async function requireAdmin(): Promise<void> {
  const supabase = await supabaseServer();
  const {
    data: { user },
  } = await supabase.auth.getUser();
  if (!isAdminEmail(user?.email)) {
    throw new Error("Not authorized.");
  }
}

export async function approveEvent(id: string): Promise<void> {
  await requireAdmin();
  const { error } = await supabaseAdmin().from("events").update({ status: "approved" }).eq("id", id);
  if (error) throw new Error(error.message);
  revalidatePath("/admin");
  revalidatePath("/");
}

/* ── daily Instagram carousel ─────────────────────────────────────────────────
   Approval only flips a status. All publishing lives in Python
   (scraper.social.publish) so that turning on IG_AUTOPOST later reuses the
   exact same code path instead of needing a second implementation here. */

export async function approveIgPost(id: string): Promise<void> {
  await requireAdmin();
  // Compare-and-swap on 'draft' (shared with the token-authed review page and
  // the Telegram webhook), so a double-click (or a stale tab) can't drag a
  // row that's already publishing back into the queue.
  const res = await approveIgPostRow(id);
  if (!res.ok) throw new Error(res.message);
  revalidatePath("/admin/ig");
}

export async function rejectIgPost(id: string): Promise<void> {
  await requireAdmin();
  const res = await rejectIgPostRow(id);
  if (!res.ok) throw new Error(res.message);
  revalidatePath("/admin/ig");
}

export async function publishIgPostNow(id: string): Promise<void> {
  await requireAdmin();
  const res = await publishIgPostNowRow(id);
  if (!res.ok) throw new Error(res.message);
  revalidatePath("/admin/ig");
}

export async function rescheduleIgPost(id: string, isoTime: string): Promise<void> {
  await requireAdmin();
  const res = await rescheduleIgPostRow(id, isoTime);
  if (!res.ok) throw new Error(res.message);
  revalidatePath("/admin/ig");
}

export async function rejectEvent(id: string): Promise<void> {
  await requireAdmin();
  // Kept, not deleted — preserves an audit trail and keeps the submitter's own
  // `events_select_own` view of it consistent (their submission didn't vanish,
  // it was reviewed and declined). `events_select_approved` already excludes
  // anything that isn't status='approved', so this never becomes publicly visible.
  const { error } = await supabaseAdmin().from("events").update({ status: "rejected" }).eq("id", id);
  if (error) throw new Error(error.message);
  revalidatePath("/admin");
}
