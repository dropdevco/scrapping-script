"use server";

import { revalidatePath } from "next/cache";
import { isAdminEmail } from "@/lib/admin";
import { supabaseAdmin } from "@/lib/supabase/admin";
import { supabaseServer } from "@/lib/supabase/server";

/* Server Actions are directly callable (not just reachable through the page's own
   UI), so each one re-checks admin auth itself — never rely solely on the page
   component's gate. */
async function requireAdmin(): Promise<void> {
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
