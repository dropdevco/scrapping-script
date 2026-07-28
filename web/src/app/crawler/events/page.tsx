import Link from "next/link";
import { fetchCrawlerEvents } from "@/lib/events";
import { eventDescription } from "@/lib/event-schema";

export const dynamic = "force-dynamic";

export default async function CrawlerEventsPage() {
  const events = await fetchCrawlerEvents().catch(() => []);

  return (
    <div className="mx-auto max-w-5xl px-4 py-12 md:px-6">
      <header className="border-b border-line pb-6">
        <p className="font-condensed text-[11px] font-semibold uppercase tracking-[0.22em] text-cosmo">
          Crawler index
        </p>
        <h1 className="mt-2 font-display text-4xl font-black italic tracking-tight text-ink">
          Upcoming Chisme events
        </h1>
        <p className="mt-3 max-w-2xl text-sm leading-relaxed text-ink-soft">
          Public index of approved upcoming event detail pages. Each linked page includes visible
          event facts, Open Graph metadata, ISO time tags, and schema.org Event JSON-LD.
        </p>
        <p className="mt-3 font-condensed text-[12px] uppercase tracking-[0.14em] text-ink-faint">
          {events.length} crawlable events
        </p>
      </header>

      <ol className="mt-8 space-y-5">
        {events.map((event) => {
          const start = event.start_time ? new Date(event.start_time) : null;
          const venueName = event.venues?.name ?? event.venue;
          const address = event.venues?.address ?? event.location;

          return (
            <li key={event.id}>
              <article className="rounded-[1.125rem] border border-line bg-card p-5">
                <h2 className="font-display text-2xl font-bold leading-tight text-ink">
                  <Link href={`/events/${event.id}`} className="underline decoration-cosmo decoration-2 underline-offset-4">
                    {event.title}
                  </Link>
                </h2>
                <dl className="mt-3 grid grid-cols-1 gap-2 text-sm text-ink-soft md:grid-cols-2">
                  <div>
                    <dt className="font-condensed text-[10px] uppercase tracking-[0.14em] text-ink-faint">
                      Start date
                    </dt>
                    <dd>
                      {start ? (
                        <time dateTime={event.start_time ?? undefined}>{start.toLocaleString("en-US")}</time>
                      ) : (
                        "Date TBA"
                      )}
                    </dd>
                  </div>
                  {venueName && (
                    <div>
                      <dt className="font-condensed text-[10px] uppercase tracking-[0.14em] text-ink-faint">
                        Venue
                      </dt>
                      <dd>{venueName}</dd>
                    </div>
                  )}
                  {address && (
                    <div>
                      <dt className="font-condensed text-[10px] uppercase tracking-[0.14em] text-ink-faint">
                        Address
                      </dt>
                      <dd>{address}</dd>
                    </div>
                  )}
                  {event.categories && event.categories.length > 0 && (
                    <div>
                      <dt className="font-condensed text-[10px] uppercase tracking-[0.14em] text-ink-faint">
                        Categories
                      </dt>
                      <dd>{event.categories.join(", ")}</dd>
                    </div>
                  )}
                </dl>
                <p className="mt-3 line-clamp-3 text-sm leading-relaxed text-ink-soft">
                  {eventDescription(event)}
                </p>
              </article>
            </li>
          );
        })}
      </ol>
    </div>
  );
}
