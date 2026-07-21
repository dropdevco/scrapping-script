import { cookies } from "next/headers";
import Link from "next/link";
import { notFound } from "next/navigation";
import { fetchEvent } from "@/lib/events";
import { dateLocale, getDict } from "@/lib/i18n";
import type { Lang } from "@/lib/types";
import { EventImage } from "@/components/event-image";

export const dynamic = "force-dynamic";

export default async function EventPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  const event = await fetchEvent(id).catch(() => null);
  if (!event) notFound();

  const cookieStore = await cookies();
  const lang = (cookieStore.get("lang")?.value === "es" ? "es" : "en") as Lang;
  const t = getDict(lang);
  const locale = dateLocale(lang);

  const start = event.start_time ? new Date(event.start_time) : null;
  const venueName = event.venues?.name ?? event.venue;
  const address = event.venues?.address ?? event.location;
  const mapsUrl = address
    ? `https://www.google.com/maps/search/?api=1&query=${encodeURIComponent(address)}`
    : null;

  return (
    <div className="mx-auto max-w-4xl px-4 pb-24 pt-10 md:px-6 md:pt-14">
      <Link
        href="/"
        className="mb-8 inline-flex items-center gap-1.5 font-condensed text-[12px] font-medium uppercase tracking-[0.14em] text-ink-soft transition-colors hover:text-cosmo"
      >
        <svg className="h-3.5 w-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
          <path d="M19 12H5m6 6-6-6 6-6" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
        {t.backToEvents}
      </Link>

      {/* kickers */}
      {event.categories && event.categories.length > 0 && (
        <p className="mb-4 flex flex-wrap gap-2">
          {event.categories.slice(0, 4).map((c) => (
            <span
              key={c}
              className="rounded-full bg-cosmo px-2.5 py-1 font-condensed text-[10px] font-bold uppercase tracking-[0.16em] text-white shadow-[2px_2px_0_var(--color-ink)]"
            >
              {c}
            </span>
          ))}
        </p>
      )}

      {/* title */}
      <h1 className="max-w-3xl font-display text-4xl font-black italic leading-[0.98] tracking-tight text-ink md:text-6xl">
        {event.title}
      </h1>

      {/* photo — double bezel on ink */}
      <div className="mt-8 rounded-[1.5rem] border-[1.5px] border-ink bg-card p-1.5 shadow-[4px_5px_0_var(--color-ink)]">
        <div className="relative aspect-[2/1] overflow-hidden rounded-[1.05rem] bg-paper-2">
          <EventImage src={event.image_url} variant="hero" className="h-full w-full object-cover" />
        </div>
      </div>

      {/* meta columns */}
      <div className="mt-8 grid grid-cols-1 gap-4 md:grid-cols-2">
        <div className="rounded-[1.125rem] border-[1.5px] border-ink bg-paper/50 p-5">
          <p className="font-condensed text-[11px] font-semibold uppercase tracking-[0.22em] text-cosmo">
            {t.when}
          </p>
          <p className="mt-2 font-display text-xl font-bold text-ink">
            {start
              ? start.toLocaleDateString(locale, { weekday: "long", month: "long", day: "numeric" })
              : t.dateTBA}
          </p>
          {start && (
            <p className="text-[14px] text-ink-soft">
              {start.toLocaleTimeString(locale, { hour: "numeric", minute: "2-digit" })}
            </p>
          )}
        </div>
        <div className="rounded-[1.125rem] border-[1.5px] border-ink bg-paper/50 p-5">
          <p className="font-condensed text-[11px] font-semibold uppercase tracking-[0.22em] text-cosmo">
            {t.where}
          </p>
          <p className="mt-2 font-display text-xl font-bold text-ink">{venueName ?? t.virtual}</p>
          {address && (
            <p className="text-[14px] leading-relaxed text-ink-soft">
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
            </p>
          )}
        </div>
      </div>

      {/* description — editorial drop-cap */}
      {event.description && (
        <div className="mt-10">
          <p className="mb-3 font-condensed text-[11px] font-semibold uppercase tracking-[0.22em] text-cosmo">
            {t.about}
          </p>
          <p className="whitespace-pre-line text-[16px] leading-relaxed text-ink/90 first-letter:float-left first-letter:mr-2 first-letter:font-display first-letter:text-5xl first-letter:font-black first-letter:italic first-letter:leading-[0.7] first-letter:text-cosmo">
            {event.description}
          </p>
        </div>
      )}

      <div className="mt-10 flex flex-wrap items-center gap-4">
        {event.url && (
          <a
            href={event.url}
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
          {t.source}: {event.source.replace("events_", "")}
        </span>
      </div>
    </div>
  );
}
