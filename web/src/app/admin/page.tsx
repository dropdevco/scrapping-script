import Link from "next/link";
import { isAdminEmail } from "@/lib/admin";
import { supabaseAdmin } from "@/lib/supabase/admin";
import { supabaseServer } from "@/lib/supabase/server";
import { approveEvent, rejectEvent } from "./actions";
import { formatEventDateTime } from "@/lib/datetime";

export const dynamic = "force-dynamic";

export default async function AdminPage() {
  const supabase = await supabaseServer();
  const {
    data: { user },
  } = await supabase.auth.getUser();

  if (!user) {
    return (
      <div className="mx-auto max-w-2xl px-4 py-16 text-center">
        <p className="text-ink-soft">Sign in to view this page.</p>
        <Link href="/" className="mt-3 inline-block underline decoration-cosmo decoration-2 underline-offset-4">
          Back home
        </Link>
      </div>
    );
  }

  if (!isAdminEmail(user.email)) {
    return (
      <div className="mx-auto max-w-2xl px-4 py-16 text-center">
        <p className="text-ink-soft">
          {user.email} is not on the moderation allowlist.
        </p>
        <Link href="/" className="mt-3 inline-block underline decoration-cosmo decoration-2 underline-offset-4">
          Back home
        </Link>
      </div>
    );
  }

  const { data: pending, error } = await supabaseAdmin()
    .from("events")
    .select("id, title, description, start_time, venue, location, url, image_url, categories, submitted_by, venues(name, address, city)")
    .eq("status", "pending")
    .order("first_seen", { ascending: false });

  if (error) {
    return <div className="mx-auto max-w-2xl px-4 py-16 text-center text-pop-red">{error.message}</div>;
  }

  return (
    <div className="mx-auto max-w-4xl px-4 pb-24 pt-10 md:px-6 md:pt-14">
      <h1 className="font-display text-4xl font-black italic tracking-tight text-ink md:text-5xl">
        Moderation queue
      </h1>
      <p className="mt-1.5 text-sm text-ink-soft">
        {pending?.length ?? 0} submission{pending?.length === 1 ? "" : "s"} awaiting review.
      </p>

      <div className="mt-8 flex flex-col gap-4">
        {(pending ?? []).map((e) => {
          // Untyped client (no generated Database types) infers a to-one FK join
          // as an array; it's always a single row (or absent) in practice here.
          const venue = Array.isArray(e.venues) ? e.venues[0] : e.venues;
          const venueName = venue?.name ?? e.venue;
          const address = venue?.address ?? e.location;
          const start = e.start_time ? new Date(e.start_time) : null;
          return (
            <article
              key={e.id}
              className="rounded-[1.25rem] border-[1.5px] border-ink bg-card p-5 shadow-[3px_3px_0_var(--color-ink)]"
            >
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div className="min-w-0">
                  <h2 className="font-display text-xl font-bold text-ink">{e.title}</h2>
                  <p className="mt-1 text-[13px] text-ink-soft">
                    {start
                      ? formatEventDateTime(e.start_time, "en-US", {
                          dateStyle: "medium",
                          timeStyle: "short",
                        })
                      : "No date given"}
                    {venueName ? ` · ${venueName}` : ""}
                    {address ? ` · ${address}` : ""}
                  </p>
                  {e.description && (
                    <p className="mt-2 max-w-xl text-[14px] text-ink/80">{e.description}</p>
                  )}
                  <p className="mt-2 flex flex-wrap gap-3 text-[12px] text-ink-faint">
                    {e.url && (
                      <a href={e.url} target="_blank" rel="noreferrer" className="underline decoration-cosmo decoration-2 underline-offset-4">
                        Event link
                      </a>
                    )}
                    {e.image_url && (
                      <a href={e.image_url} target="_blank" rel="noreferrer" className="underline decoration-cosmo decoration-2 underline-offset-4">
                        Image
                      </a>
                    )}
                    <span>submitted_by: {e.submitted_by}</span>
                  </p>
                </div>

                <div className="flex shrink-0 gap-2">
                  <form action={approveEvent.bind(null, e.id)}>
                    <button
                      type="submit"
                      className="rounded-full bg-cosmo px-4 py-1.5 text-sm font-semibold text-white shadow-[2px_2px_0_var(--color-ink)] transition-transform duration-200 hover:-translate-y-0.5"
                    >
                      Approve
                    </button>
                  </form>
                  <form action={rejectEvent.bind(null, e.id)}>
                    <button
                      type="submit"
                      className="rounded-full border-[1.5px] border-ink bg-paper px-4 py-1.5 text-sm font-semibold text-ink transition-transform duration-200 hover:-translate-y-0.5"
                    >
                      Reject
                    </button>
                  </form>
                </div>
              </div>
            </article>
          );
        })}

        {(pending ?? []).length === 0 && (
          <div className="rounded-[1.5rem] border-[1.5px] border-dashed border-ink/40 bg-card p-12 text-center text-ink-soft">
            Nothing waiting on review.
          </div>
        )}
      </div>
    </div>
  );
}
