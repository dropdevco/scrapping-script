"use client";

import Link from "next/link";
import { motion } from "motion/react";
import type { EventRow } from "@/lib/types";
import { useLang } from "./lang-context";
import { dateLocale } from "@/lib/i18n";
import { EventImage } from "./event-image";

function fmtDate(iso: string | null, locale: string): { day: string; time: string } | null {
  if (!iso) return null;
  const d = new Date(iso);
  return {
    day: d.toLocaleDateString(locale, { weekday: "short", month: "short", day: "numeric" }),
    time: d.toLocaleTimeString(locale, { hour: "numeric", minute: "2-digit" }),
  };
}

export function EventCard({ event, index }: { event: EventRow; index: number }) {
  const { lang, t } = useLang();
  const when = fmtDate(event.start_time, dateLocale(lang));
  const venueName = event.venues?.name ?? event.venue;
  const city = event.venues?.city;

  return (
    <motion.article
      initial={{ opacity: 0, y: 14 }}
      whileInView={{ opacity: 1, y: 0 }}
      viewport={{ once: true, margin: "-40px" }}
      transition={{ duration: 0.45, delay: Math.min(index % 9, 6) * 0.04, ease: [0.16, 1, 0.3, 1] }}
      className="group h-full"
    >
      <Link
        href={`/events/${event.id}`}
        className="flex h-full flex-col rounded-[1.75rem] bg-surface p-1.5 ring-1 ring-line/70 transition-all duration-300 ease-[cubic-bezier(0.16,1,0.3,1)] hover:-translate-y-0.5 hover:ring-sand-faint/50"
      >
        {/* image */}
        <div className="relative aspect-[16/9] overflow-hidden rounded-[1.375rem] bg-surface-2">
          <EventImage
            src={event.image_url}
            variant="card"
            className="h-full w-full object-cover transition-transform duration-500 ease-[cubic-bezier(0.16,1,0.3,1)] group-hover:scale-[1.03]"
          />
          {when && (
            <span className="absolute left-2.5 top-2.5 rounded-full bg-night/85 px-2.5 py-1 text-[11px] font-medium text-sand backdrop-blur-sm">
              {when.day}
            </span>
          )}
        </div>

        {/* body */}
        <div className="flex flex-1 flex-col gap-1.5 px-3.5 pb-3.5 pt-3">
          <h3 className="font-display text-[15px] font-semibold leading-snug text-sand [display:-webkit-box] [-webkit-box-orient:vertical] [-webkit-line-clamp:2] overflow-hidden">
            {event.title}
          </h3>
          <p className="text-[13px] text-sand-dim">
            {when ? `${when.time}` : t.dateTBA}
            {venueName ? (
              <>
                {" · "}
                <span className="text-sand-dim">{venueName}</span>
              </>
            ) : null}
          </p>
          <div className="mt-auto flex items-center justify-between pt-1.5">
            {city ? (
              <span className="rounded-full border border-line px-2 py-0.5 text-[10px] uppercase tracking-[0.14em] text-sand-faint">
                {city}
              </span>
            ) : (
              <span className="text-[10px] uppercase tracking-[0.14em] text-sand-faint">
                {event.location ? "" : t.virtual}
              </span>
            )}
            {event.categories?.[0] && (
              <span className="text-[10px] uppercase tracking-[0.14em] text-sunset-soft">
                {event.categories[0]}
              </span>
            )}
          </div>
        </div>
      </Link>
    </motion.article>
  );
}
