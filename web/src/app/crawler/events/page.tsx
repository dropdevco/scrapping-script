import Link from "next/link";
import { fetchCrawlerEvents } from "@/lib/events";
import { eventDescription, eventImageUrl } from "@/lib/event-schema";
import { formatEventDateTime } from "@/lib/datetime";
import type { EventRow } from "@/lib/types";

export const dynamic = "force-dynamic";

/*
  This page exists to be trained on by external AI knowledge-base crawlers
  (GoHighLevel etc). With every event's full description on one URL, the
  page runs to 500KB+/60K+ visible characters — most of these tools cap how
  much content they ingest from a SINGLE page and silently drop the rest,
  which is why some events never made it into training even though they're
  right there in the HTML.

  Fix: paginate by CONTENT SIZE rather than a fixed event count, so no page
  ever blows past a safe per-page budget no matter how long individual
  descriptions get, and link real <a>/<Link> pagination between pages so any
  link-following crawler discovers all of them from the one seed URL.
*/
const MAX_CHARS_PER_PAGE = 25_000;

function paginateByContentSize(events: EventRow[]): EventRow[][] {
  const pages: EventRow[][] = [];
  let current: EventRow[] = [];
  let currentChars = 0;

  for (const event of events) {
    // Rough weight of this event's rendered text; 200 covers the date/venue/
    // address/categories/image-url boilerplate around the description.
    const weight = event.title.length + eventDescription(event).length + 200;
    if (current.length > 0 && currentChars + weight > MAX_CHARS_PER_PAGE) {
      pages.push(current);
      current = [];
      currentChars = 0;
    }
    current.push(event);
    currentChars += weight;
  }
  if (current.length > 0) pages.push(current);
  // Always return at least one (possibly empty) page — an empty event list
  // shouldn't produce zero pages and break page-number math below.
  return pages.length > 0 ? pages : [[]];
}

type CrawlerPageProps = { searchParams: Promise<{ page?: string }> };

export default async function CrawlerEventsPage({ searchParams }: CrawlerPageProps) {
  const sp = await searchParams;
  const allEvents = await fetchCrawlerEvents().catch(() => []);
  const pages = paginateByContentSize(allEvents);
  const totalPages = pages.length;
  const requestedPage = Number(sp.page) || 1;
  const currentPage = Math.min(Math.max(requestedPage, 1), totalPages);
  const events = pages[currentPage - 1] ?? [];

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
          {allEvents.length} crawlable events
          {totalPages > 1 && ` — page ${currentPage} of ${totalPages}`}
        </p>
      </header>

      <ol className="mt-8 space-y-5">
        {events.map((event) => {
          const start = event.start_time ? new Date(event.start_time) : null;
          const venueName = event.venues?.name ?? event.venue;
          const address = event.venues?.address ?? event.location;
          const image = eventImageUrl(event);

          return (
            <li key={event.id}>
              <article className="rounded-[1.125rem] border border-line bg-card p-5">
                {image && (
                  // Plain server-rendered <img>, not the client EventImage
                  // component: this page exists for scrapers, and a real src
                  // attribute in the initial HTML is what they read — a
                  // client component that swaps the src after hydration (its
                  // broken-image fallback) buys nothing here and only risks
                  // scrapers that don't run JS seeing an empty tag.
                  // eslint-disable-next-line @next/next/no-img-element
                  <img
                    src={image}
                    alt={`${event.title} event image`}
                    loading="lazy"
                    className="mb-4 aspect-[2/1] w-full rounded-[0.75rem] object-cover"
                  />
                )}
                <h2 className="font-display text-2xl font-bold leading-tight text-ink">
                  <Link href={`/events/${event.id}`} className="underline decoration-cosmo decoration-2 underline-offset-4">
                    {event.title}
                  </Link>
                </h2>
                <dl className="mt-3 grid grid-cols-1 gap-2 text-sm text-ink-soft md:grid-cols-2">
                  {image && (
                    <div className="md:col-span-2">
                      <dt className="font-condensed text-[10px] uppercase tracking-[0.14em] text-ink-faint">
                        Image URL
                      </dt>
                      <dd>
                        <a href={image} className="break-all underline decoration-cosmo decoration-2 underline-offset-4">
                          {image}
                        </a>
                      </dd>
                    </div>
                  )}
                  <div>
                    <dt className="font-condensed text-[10px] uppercase tracking-[0.14em] text-ink-faint">
                      Start date
                    </dt>
                    <dd>
                      {start ? (
                        <time dateTime={event.start_time ?? undefined}>{formatEventDateTime(event.start_time, "en-US")}</time>
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
                {/* No line-clamp here on purpose: -webkit-line-clamp uses
                    overflow:hidden, and headless-browser scrapers that read
                    rendered/visible text (innerText, not textContent) stop
                    exactly at the visual cutoff — this page exists to be
                    machine-read in full, so nothing here should be clipped. */}
                <p className="mt-3 whitespace-pre-line text-sm leading-relaxed text-ink-soft">
                  {eventDescription(event)}
                </p>
              </article>
            </li>
          );
        })}
      </ol>

      {totalPages > 1 && (
        // Plain <Link>/<a href> pagination — no query-string-only JS nav —
        // so a link-following crawler can walk every page starting from
        // just this one seed URL.
        <nav aria-label="Pagination" className="mt-10 flex items-center justify-between border-t border-line pt-6">
          {currentPage > 1 ? (
            <Link
              href={`/crawler/events?page=${currentPage - 1}`}
              className="font-condensed text-[12px] font-semibold uppercase tracking-[0.14em] text-ink underline decoration-cosmo decoration-2 underline-offset-4"
            >
              ← Previous page
            </Link>
          ) : (
            <span />
          )}
          <span className="font-condensed text-[12px] uppercase tracking-[0.14em] text-ink-faint">
            Page {currentPage} of {totalPages}
          </span>
          {currentPage < totalPages ? (
            <Link
              href={`/crawler/events?page=${currentPage + 1}`}
              className="font-condensed text-[12px] font-semibold uppercase tracking-[0.14em] text-ink underline decoration-cosmo decoration-2 underline-offset-4"
            >
              Next page →
            </Link>
          ) : (
            <span />
          )}
        </nav>
      )}
    </div>
  );
}
