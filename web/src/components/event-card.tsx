"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { motion } from "motion/react";
import type { EventRow } from "@/lib/types";
import { useLang } from "./lang-context";
import { dateLocale } from "@/lib/i18n";
import { EventImage } from "./event-image";
import { canonicalCategoriesForEvent } from "@/lib/categories";

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
  const searchParams = useSearchParams();
  const when = fmtDate(event.start_time, dateLocale(lang));
  const venueName = event.venues?.name ?? event.venue;
  const city = event.venues?.city;
  const rawCategories = event.categories?.filter(Boolean) ?? [];
  const categories = canonicalCategoriesForEvent(rawCategories);
  const selectedCategories = (searchParams.get("categories") ?? "")
    .split(",")
    .map((category) => category.trim())
    .filter(Boolean);
  const category = selectedCategories.find((selected) => categories.includes(selected)) ?? categories[0];
  const hasMoreCategories = rawCategories.length > 1 || categories.length > 1;

  return (
    <motion.article
      initial={{ opacity: 0, y: 16 }}
      whileInView={{ opacity: 1, y: 0 }}
      viewport={{ once: true, margin: "-40px" }}
      transition={{ duration: 0.45, delay: Math.min(index % 8, 6) * 0.04, ease: [0.16, 1, 0.3, 1] }}
      className="group h-full"
    >
      <Link
        href={`/events/${event.id}`}
        className="flex h-full flex-col rounded-[1.25rem] border-[1.5px] border-ink bg-card p-1.5 shadow-[3px_3px_0_var(--color-ink)] transition-transform duration-300 ease-[cubic-bezier(0.16,1,0.3,1)] hover:-translate-y-1 hover:-rotate-[0.7deg] hover:shadow-[5px_6px_0_var(--color-cosmo)]"
      >
        {/* clipping photo */}
        <div className="relative aspect-[16/10] overflow-hidden rounded-[0.85rem] bg-paper-2">
          <EventImage
            src={event.image_url}
            alt={event.title}
            variant="card"
            className="h-full w-full object-cover transition-transform duration-500 ease-[cubic-bezier(0.16,1,0.3,1)] group-hover:scale-[1.04]"
          />
          {when && (
            <span className="absolute left-2 top-2 rounded-full bg-ink px-2.5 py-1 font-condensed text-[11px] font-semibold uppercase tracking-[0.08em] text-paper">
              {when.day}
            </span>
          )}
          {category && (
            <div className="absolute bottom-2 left-2 flex max-w-[calc(100%-1rem)] items-center gap-1.5">
              <span className="min-w-0 truncate rounded-full bg-cosmo px-2.5 py-1 font-condensed text-[10px] font-bold uppercase tracking-[0.14em] text-white shadow-[2px_2px_0_var(--color-ink)]">
                {category}
              </span>
              {hasMoreCategories && (
                <span
                  aria-label={`${rawCategories.length} categories`}
                  title={rawCategories.join(", ")}
                  className="flex h-6 w-6 shrink-0 items-center justify-center rounded-full border-[1.5px] border-ink bg-cosmo font-condensed text-[15px] font-bold leading-none text-white shadow-[2px_2px_0_var(--color-ink)]"
                >
                  +
                </span>
              )}
            </div>
          )}
        </div>

        {/* body */}
        <div className="flex flex-1 flex-col gap-1.5 px-2.5 pb-2.5 pt-3">
          <h3 className="font-display text-[17px] font-bold leading-[1.12] tracking-tight text-ink [display:-webkit-box] [-webkit-box-orient:vertical] [-webkit-line-clamp:2] overflow-hidden">
            {event.title}
          </h3>
          <p className="text-[13px] text-ink-soft">
            {when ? when.time : t.dateTBA}
            {venueName ? (
              <>
                {" · "}
                <span>{venueName}</span>
              </>
            ) : null}
          </p>
          <div className="mt-auto flex items-center justify-between pt-2">
            {city ? (
              <span className="rounded-full border border-ink/25 px-2 py-0.5 font-condensed text-[10px] font-medium uppercase tracking-[0.14em] text-ink-faint">
                {city}
              </span>
            ) : (
              <span className="font-condensed text-[10px] uppercase tracking-[0.14em] text-ink-faint">
                {event.location ? "" : t.virtual}
              </span>
            )}
            <span className="flex h-6 w-6 items-center justify-center rounded-full border border-ink/20 text-ink-faint transition-colors group-hover:border-cosmo group-hover:text-cosmo">
              <svg className="h-3 w-3" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2">
                <path d="M5 12h14m-6-6 6 6-6 6" strokeLinecap="round" strokeLinejoin="round" />
              </svg>
            </span>
          </div>
        </div>
      </Link>
    </motion.article>
  );
}
