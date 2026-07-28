import type { Metadata } from "next";
import { cookies } from "next/headers";
import Link from "next/link";
import { notFound } from "next/navigation";
import { EventImage } from "@/components/event-image";
import { buildEventJsonLd, eventDescription, eventImageUrl, jsonLdScript } from "@/lib/event-schema";
import { fetchEvent } from "@/lib/events";
import { dateLocale, getDict } from "@/lib/i18n";
import { absoluteUrl, eventUrl } from "@/lib/site";
import type { Lang } from "@/lib/types";

export const dynamic = "force-dynamic";

type EventPageProps = { params: Promise<{ id: string }> };

export async function generateMetadata({ params }: EventPageProps): Promise<Metadata> {
  const { id } = await params;
  const event = await fetchEvent(id).catch(() => null);
  if (!event) {
    return {
      title: "Event not found | Chisme",
      robots: { index: false, follow: false },
    };
  }

  const description = eventDescription(event).slice(0, 180);
  const image = eventImageUrl(event);
  const url = eventUrl(event.id);

  return {
    title: `${event.title} | Chisme`,
    description,
    alternates: { canonical: url },
    openGraph: {
      type: "article",
      title: event.title,
      description,
      url,
      siteName: "Chisme",
      images: image ? [{ url: image, alt: event.title }] : undefined,
    },
    twitter: {
      card: image ? "summary_large_image" : "summary",
      title: event.title,
      description,
      images: image ? [image] : undefined,
    },
  };
}

export default async function EventPage({ params }: EventPageProps) {
  const { id } = await params;
  const event = await fetchEvent(id).catch(() => null);
  if (!event) notFound();

  const cookieStore = await cookies();
  const lang = (cookieStore.get("lang")?.value === "es" ? "es" : "en") as Lang;
  const t = getDict(lang);
  const locale = dateLocale(lang);

  const start = event.start_time ? new Date(event.start_time) : null;
  const end = event.end_time ? new Date(event.end_time) : null;
  const venueName = event.venues?.name ?? event.venue;
  const address = event.venues?.address ?? event.location;
  const city = event.venues?.city;
  const region = event.venues?.region;
  const sourceName = event.source.replace("events_", "");
  const canonicalUrl = eventUrl(event.id);
  const ticketUrl = absoluteUrl(event.url);
  const jsonLd = buildEventJsonLd(event);
  const mapsUrl = address
    ? `https://www.google.com/maps/search/?api=1&query=${encodeURIComponent(address)}`
    : null;

  return (
    <article
      itemScope
      itemType="https://schema.org/Event"
      className="mx-auto max-w-4xl px-4 pb-24 pt-10 md:px-6 md:pt-14"
    >
      <script
        type="application/ld+json"
        dangerouslySetInnerHTML={{ __html: jsonLdScript(jsonLd) }}
      />

      <Link
        href="/"
        className="mb-8 inline-flex items-center gap-1.5 font-condensed text-[12px] font-medium uppercase tracking-[0.14em] text-ink-soft transition-colors hover:text-cosmo"
      >
        <svg className="h-3.5 w-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
          <path d="M19 12H5m6 6-6-6 6-6" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
        {t.backToEvents}
      </Link>

      <header>
        {event.categories && event.categories.length > 0 && (
          <ul className="mb-4 flex flex-wrap gap-2" aria-label="Event categories">
            {event.categories.slice(0, 4).map((c) => (
              <li
                key={c}
                className="rounded-full bg-cosmo px-2.5 py-1 font-condensed text-[10px] font-bold uppercase tracking-[0.16em] text-white shadow-[2px_2px_0_var(--color-ink)]"
              >
                {c}
              </li>
            ))}
          </ul>
        )}

        <h1
          itemProp="name"
          className="max-w-3xl font-display text-4xl font-black italic leading-[0.98] tracking-tight text-ink md:text-6xl"
        >
          {event.title}
        </h1>
      </header>

      <div className="mt-8 rounded-[1.5rem] border-[1.5px] border-ink bg-card p-1.5 shadow-[4px_5px_0_var(--color-ink)]">
        <div className="relative aspect-[2/1] overflow-hidden rounded-[1.05rem] bg-paper-2">
          <EventImage
            src={event.image_url}
            alt={`${event.title} event image`}
            variant="hero"
            className="h-full w-full object-cover"
          />
        </div>
      </div>

      <section aria-label="Event details" className="mt-8 grid grid-cols-1 gap-4 md:grid-cols-2">
        <div className="rounded-[1.125rem] border-[1.5px] border-ink bg-paper/50 p-5">
          <p className="font-condensed text-[11px] font-semibold uppercase tracking-[0.22em] text-cosmo">
            {t.when}
          </p>
          <dl className="mt-2 space-y-2">
            <div>
              <dt className="font-condensed text-[10px] uppercase tracking-[0.14em] text-ink-faint">
                Date
              </dt>
              <dd className="font-display text-xl font-bold text-ink">
                {start ? (
                  <time itemProp="startDate" dateTime={event.start_time ?? undefined}>
                    {start.toLocaleDateString(locale, {
                      weekday: "long",
                      month: "long",
                      day: "numeric",
                    })}
                  </time>
                ) : (
                  t.dateTBA
                )}
              </dd>
            </div>
            {start && (
              <div>
                <dt className="font-condensed text-[10px] uppercase tracking-[0.14em] text-ink-faint">
                  Start time
                </dt>
                <dd className="text-[14px] text-ink-soft">
                  <time dateTime={event.start_time ?? undefined}>
                    {start.toLocaleTimeString(locale, { hour: "numeric", minute: "2-digit" })}
                  </time>
                </dd>
              </div>
            )}
            {end && (
              <div>
                <dt className="font-condensed text-[10px] uppercase tracking-[0.14em] text-ink-faint">
                  End time
                </dt>
                <dd className="text-[14px] text-ink-soft">
                  <time itemProp="endDate" dateTime={event.end_time ?? undefined}>
                    {end.toLocaleTimeString(locale, { hour: "numeric", minute: "2-digit" })}
                  </time>
                </dd>
              </div>
            )}
          </dl>
        </div>

        <div className="rounded-[1.125rem] border-[1.5px] border-ink bg-paper/50 p-5">
          <p className="font-condensed text-[11px] font-semibold uppercase tracking-[0.22em] text-cosmo">
            {t.where}
          </p>
          <dl className="mt-2 space-y-2" itemProp="location" itemScope itemType="https://schema.org/Place">
            <div>
              <dt className="font-condensed text-[10px] uppercase tracking-[0.14em] text-ink-faint">
                Venue
              </dt>
              <dd itemProp="name" className="font-display text-xl font-bold text-ink">
                {venueName ?? t.virtual}
              </dd>
            </div>
            {address && (
              <div>
                <dt className="font-condensed text-[10px] uppercase tracking-[0.14em] text-ink-faint">
                  Address
                </dt>
                <dd itemProp="address" className="text-[14px] leading-relaxed text-ink-soft">
                  {mapsUrl ? (
                    <a
                      href={mapsUrl}
                      target="_blank"
                      rel="noreferrer"
                      className="underline decoration-cosmo decoration-2 underline-offset-4 transition-colors hover:text-cosmo"
                    >
                      {address}
                    </a>
                  ) : (
                    address
                  )}
                </dd>
              </div>
            )}
            {(city || region) && (
              <div>
                <dt className="font-condensed text-[10px] uppercase tracking-[0.14em] text-ink-faint">
                  City
                </dt>
                <dd className="text-[14px] text-ink-soft">
                  {[city, region].filter(Boolean).join(", ")}
                </dd>
              </div>
            )}
          </dl>
        </div>
      </section>

      {event.description && (
        <section className="mt-10">
          <p className="mb-3 font-condensed text-[11px] font-semibold uppercase tracking-[0.22em] text-cosmo">
            {t.about}
          </p>
          <p
            itemProp="description"
            className="whitespace-pre-line text-[16px] leading-relaxed text-ink/90 first-letter:float-left first-letter:mr-2 first-letter:font-display first-letter:text-5xl first-letter:font-black first-letter:italic first-letter:leading-[0.7] first-letter:text-cosmo"
          >
            {event.description}
          </p>
        </section>
      )}

      <footer className="mt-10">
        <div className="flex flex-wrap items-center gap-4">
          {ticketUrl && (
            <a
              href={ticketUrl}
              target="_blank"
              rel="noreferrer"
              className="group inline-flex items-center gap-2 rounded-full bg-cosmo py-1.5 pl-5 pr-1.5 text-sm font-semibold text-white shadow-[3px_3px_0_var(--color-ink)] transition-transform duration-300 ease-[cubic-bezier(0.16,1,0.3,1)] hover:-translate-y-0.5"
            >
              {t.getTickets}
              <span className="flex h-8 w-8 items-center justify-center rounded-full bg-ink text-white transition-transform duration-300 group-hover:translate-x-0.5">
                <svg className="h-3.5 w-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                  <path d="M7 17 17 7m0 0H8m9 0v9" strokeLinecap="round" strokeLinejoin="round" />
                </svg>
              </span>
            </a>
          )}
          <span className="font-condensed text-[11px] uppercase tracking-[0.14em] text-ink-faint">
            {t.source}: {sourceName}
          </span>
        </div>

        <dl className="mt-5 grid grid-cols-1 gap-2 rounded-[1.125rem] border border-line bg-paper/40 p-4 text-[12px] text-ink-soft sm:grid-cols-2">
          <div>
            <dt className="font-condensed uppercase tracking-[0.14em] text-ink-faint">Event URL</dt>
            <dd>
              <a href={canonicalUrl} className="break-all underline decoration-cosmo decoration-2 underline-offset-4">
                {canonicalUrl}
              </a>
            </dd>
          </div>
          {ticketUrl && (
            <div>
              <dt className="font-condensed uppercase tracking-[0.14em] text-ink-faint">Ticket link</dt>
              <dd>
                <a href={ticketUrl} className="break-all underline decoration-cosmo decoration-2 underline-offset-4">
                  {ticketUrl}
                </a>
              </dd>
            </div>
          )}
          {event.categories && event.categories.length > 0 && (
            <div>
              <dt className="font-condensed uppercase tracking-[0.14em] text-ink-faint">Categories</dt>
              <dd>{event.categories.join(", ")}</dd>
            </div>
          )}
          <div>
            <dt className="font-condensed uppercase tracking-[0.14em] text-ink-faint">Source</dt>
            <dd>{sourceName}</dd>
          </div>
        </dl>
      </footer>
    </article>
  );
}
