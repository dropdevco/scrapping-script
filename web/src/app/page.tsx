import { Suspense } from "react";
import { cookies } from "next/headers";
import Link from "next/link";
import { fetchCategories, fetchEvents } from "@/lib/events";
import { getDict } from "@/lib/i18n";
import type { Lang } from "@/lib/types";
import { Filters } from "@/components/filters";
import { EventCard } from "@/components/event-card";

export const dynamic = "force-dynamic";

type Search = { [key: string]: string | string[] | undefined };

async function EventGrid({ searchParams }: { searchParams: Search }) {
  const cookieStore = await cookies();
  const lang = (cookieStore.get("lang")?.value === "es" ? "es" : "en") as Lang;
  const t = getDict(lang);

  const one = (v: string | string[] | undefined) => (Array.isArray(v) ? v[0] : v);
  let events;
  try {
    events = await fetchEvents({
      q: one(searchParams.q),
      city: one(searchParams.city),
      when: one(searchParams.when),
      category: one(searchParams.category),
    });
  } catch (err) {
    // One failed query must never take down the whole page — hero and
    // filters stay useful; the grid degrades to a friendly message.
    console.error("EventGrid:", err);
    return (
      <div className="rounded-[1.75rem] bg-surface p-12 text-center ring-1 ring-line/70">
        <p className="text-sand-dim">{t.errGeneric}</p>
      </div>
    );
  }

  if (events.length === 0) {
    return (
      <div className="rounded-[1.75rem] bg-surface p-12 text-center ring-1 ring-line/70">
        <p className="text-sand-dim">{t.noEvents}</p>
      </div>
    );
  }

  return (
    <>
      <p className="mb-5 text-[13px] text-sand-faint">
        {events.length} {events.length === 1 ? t.eventFound : t.eventsFound}
      </p>
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
        {events.map((e, i) => (
          <EventCard key={e.id} event={e} index={i} />
        ))}
      </div>
    </>
  );
}

export default async function Home({ searchParams }: { searchParams: Promise<Search> }) {
  const sp = await searchParams;
  const cookieStore = await cookies();
  const lang = (cookieStore.get("lang")?.value === "es" ? "es" : "en") as Lang;
  const t = getDict(lang);
  const categories = await fetchCategories().catch(() => []);

  return (
    <div className="mx-auto max-w-6xl px-4 md:px-6">
      {/* hero */}
      <section className="pb-14 pt-16 md:pb-20 md:pt-28">
        <p className="mb-4 inline-block rounded-full border border-line px-3 py-1 text-[10px] uppercase tracking-[0.2em] text-sand-dim">
          {t.tagline}
        </p>
        <h1 className="max-w-3xl font-display text-5xl font-bold leading-[1.02] tracking-tight text-sand md:text-7xl">
          {t.heroTitle}{" "}
          <span className="bg-gradient-to-r from-sunset to-rose-dusk bg-clip-text text-transparent">
            {t.heroTitleAccent}
          </span>
        </h1>
        <p className="mt-5 max-w-xl text-[15px] leading-relaxed text-sand-dim md:text-base">
          {t.heroSub}
        </p>
        <div className="mt-8 flex items-center gap-3">
          <a
            href="#events"
            className="group flex items-center gap-2 rounded-full bg-sand py-1.5 pl-5 pr-1.5 text-sm font-medium text-night transition-transform duration-300 ease-[cubic-bezier(0.16,1,0.3,1)] hover:scale-[1.02]"
          >
            {t.exploreEvents}
            <span className="flex h-8 w-8 items-center justify-center rounded-full bg-night text-sand transition-transform duration-300 group-hover:translate-x-0.5">
              <svg className="h-3.5 w-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                <path d="M5 12h14m-6-6 6 6-6 6" strokeLinecap="round" strokeLinejoin="round" />
              </svg>
            </span>
          </a>
          <Link
            href="/map"
            className="rounded-full border border-line px-5 py-2.5 text-sm text-sand-dim transition-colors duration-200 hover:border-sand-faint hover:text-sand"
          >
            {t.viewMap}
          </Link>
        </div>
      </section>

      {/* filters + grid */}
      <section id="events" className="scroll-mt-24 pb-24">
        <Suspense>
          <Filters categories={categories} />
        </Suspense>
        <div className="mt-8">
          <Suspense
            key={JSON.stringify(sp)}
            fallback={
              <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
                {Array.from({ length: 6 }).map((_, i) => (
                  <div
                    key={i}
                    className="aspect-[4/3] animate-pulse rounded-[1.75rem] bg-surface ring-1 ring-line/40"
                  />
                ))}
              </div>
            }
          >
            <EventGrid searchParams={sp} />
          </Suspense>
        </div>
      </section>
    </div>
  );
}
