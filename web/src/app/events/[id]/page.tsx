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
    <div className="mx-auto max-w-4xl px-4 pb-24 pt-10 md:px-6 md:pt-16">
      <Link
        href="/"
        className="mb-8 inline-flex items-center gap-1.5 text-[13px] text-sand-faint transition-colors hover:text-sand"
      >
        <svg className="h-3.5 w-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
          <path d="M19 12H5m6 6-6-6 6-6" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
        {t.backToEvents}
      </Link>

      {/* hero card — double bezel */}
      <div className="rounded-[1.75rem] bg-surface p-1.5 ring-1 ring-line/70">
        <div className="relative aspect-[2/1] overflow-hidden rounded-[1.375rem] bg-surface-2">
          <EventImage src={event.image_url} variant="hero" className="h-full w-full object-cover" />
        </div>

        <div className="px-5 pb-6 pt-5 md:px-7 md:pb-8">
          {event.categories && event.categories.length > 0 && (
            <p className="mb-3 flex flex-wrap gap-2">
              {event.categories.slice(0, 3).map((c) => (
                <span
                  key={c}
                  className="rounded-full border border-line px-2.5 py-0.5 text-[10px] uppercase tracking-[0.16em] text-sunset-soft"
                >
                  {c}
                </span>
              ))}
            </p>
          )}
          <h1 className="font-display text-3xl font-bold leading-tight tracking-tight text-sand md:text-4xl">
            {event.title}
          </h1>

          <div className="mt-6 grid grid-cols-1 gap-4 md:grid-cols-2">
            <div className="rounded-[1.125rem] bg-surface-2 p-4 ring-1 ring-line/50">
              <p className="text-[10px] uppercase tracking-[0.18em] text-sand-faint">{t.when}</p>
              <p className="mt-1.5 text-[15px] text-sand">
                {start
                  ? start.toLocaleDateString(locale, {
                      weekday: "long",
                      month: "long",
                      day: "numeric",
                    })
                  : t.dateTBA}
              </p>
              {start && (
                <p className="text-[13px] text-sand-dim">
                  {start.toLocaleTimeString(locale, { hour: "numeric", minute: "2-digit" })}
                </p>
              )}
            </div>
            <div className="rounded-[1.125rem] bg-surface-2 p-4 ring-1 ring-line/50">
              <p className="text-[10px] uppercase tracking-[0.18em] text-sand-faint">{t.where}</p>
              <p className="mt-1.5 text-[15px] text-sand">{venueName ?? t.virtual}</p>
              {address && (
                <p className="text-[13px] leading-relaxed text-sand-dim">
                  {mapsUrl ? (
                    <a href={mapsUrl} target="_blank" rel="noreferrer" className="underline decoration-line underline-offset-4 transition-colors hover:text-sand">
                      {address}
                    </a>
                  ) : (
                    address
                  )}
                </p>
              )}
            </div>
          </div>

          {event.description && (
            <div className="mt-6">
              <p className="text-[10px] uppercase tracking-[0.18em] text-sand-faint">{t.about}</p>
              <p className="mt-2 whitespace-pre-line text-[15px] leading-relaxed text-sand-dim">
                {event.description}
              </p>
            </div>
          )}

          <div className="mt-8 flex flex-wrap items-center gap-3">
            {event.url && (
              <a
                href={event.url}
                target="_blank"
                rel="noreferrer"
                className="group flex items-center gap-2 rounded-full bg-sand py-1.5 pl-5 pr-1.5 text-sm font-medium text-night transition-transform duration-300 ease-[cubic-bezier(0.16,1,0.3,1)] hover:scale-[1.02]"
              >
                {t.getTickets}
                <span className="flex h-8 w-8 items-center justify-center rounded-full bg-night text-sand transition-transform duration-300 group-hover:translate-x-0.5">
                  <svg className="h-3.5 w-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                    <path d="M7 17 17 7m0 0H8m9 0v9" strokeLinecap="round" strokeLinejoin="round" />
                  </svg>
                </span>
              </a>
            )}
            <span className="text-[11px] text-sand-faint">
              {t.source}: {event.source.replace("events_", "")}
            </span>
          </div>
        </div>
      </div>
    </div>
  );
}
