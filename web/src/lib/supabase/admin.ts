import { createClient } from "@supabase/supabase-js";

/* Service-role client — bypasses RLS entirely. Server-only: SUPABASE_SERVICE_ROLE_KEY
   has no NEXT_PUBLIC_ prefix, so Next.js never ships it to the browser bundle. Never
   import this from a "use client" component.

   Needed because the moderation queue has to read/write EVERY user's pending
   submissions, not just the signed-in admin's own rows — the normal anon-key client
   (RLS: `events_select_own`) can only see your own submissions. */
export function supabaseAdmin() {
  const url = process.env.NEXT_PUBLIC_SUPABASE_URL;
  const key = process.env.SUPABASE_SERVICE_ROLE_KEY;
  if (!url || !key) {
    throw new Error("SUPABASE_SERVICE_ROLE_KEY is not configured — admin actions are disabled.");
  }
  return createClient(url, key, { auth: { persistSession: false } });
}
