import { Suspense } from "react";
import { cookies } from "next/headers";
import Link from "next/link";
import { fetchCategories, fetchEvents } from "@/lib/events";
import { getDict } from "@/lib/i18n";
import type { Lang } from "@/lib/types";
import { Filters } from "@/components/filters";
import { EventCard } from "@/components/event-card";
import { CutoutText } from "@/components/cutout-text";

export const dynamic = "force-dynamic";

type Search = { [key: string]: string | string[] | undefined };

const one = (v: string | string[] | undefined) => (Array.isArray(v) ? v[0] : v);
function many(v: string | string[] | undefined): string[] {
  const s = Array.isArray(v) ? v.join(",") : (v ?? "");
  return s.split(",").map((x) => x.trim()).filter(Boolean);
}

async function EventGrid({ searchParams }: { searchParams: Search }) {
  const cookieStore = await cookies();
  const lang = (cookieStore.get("lang")?.value === "es" ? "es" : "en") as Lang;
  const t = getDict(lang);

  let events;
  try {
    events = await fetchEvents({
      q: one(searchParams.q),
      city: one(searchParams.city),
      when: one(searchParams.when),
      categories: many(searchParams.categories),
    });
  } catch (err) {
    console.error("EventGrid:", err);
    return (
      <div className="rounded-[1.5rem] border-[1.5px] border-ink bg-card p-12 text-center">
        <p className="text-ink-soft">{t.errGeneric}</p>
      </div>
    );
  }

  if (events.length === 0) {
    return (
      <div className="rounded-[1.5rem] border-[1.5px] border-dashed border-ink/40 bg-card p-12 text-center">
        <p className="text-ink-soft">{t.noEvents}</p>
      </div>
    );
  }

  return (
    <>
      <p className="mb-5 font-condensed text-[13px] font-medium uppercase tracking-[0.16em] text-ink-soft">
        {events.length} {events.length === 1 ? t.eventFound : t.eventsFound}
      </p>
      <div className="grid grid-cols-1 gap-5 sm:grid-cols-2 xl:grid-cols-3">
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
    <>
      {/* ── HERO — magazine cover-line over the landmark collage ─────────── */}
      <section className="relative min-h-[78vh]">
        <div className="relative z-10 mx-auto max-w-6xl px-4 pt-16 md:px-6 md:pt-24">
          {/* cover-line */}
          <h1 className="max-w-3xl font-display text-[13vw] font-black italic leading-[0.9] tracking-tight text-ink sm:text-7xl md:text-8xl">
            <span className="block">{t.heroTitle}</span>
            <CutoutText
              text={t.heroTitleAccent}
              seed={42}
              className="mt-3 text-[10vw] not-italic leading-none sm:text-6xl md:text-7xl"
            />
          </h1>

          <p className="mt-6 max-w-md text-[15px] leading-relaxed text-ink-soft md:text-base">
            {t.heroSub}
          </p>

          <div className="mt-8 flex flex-wrap items-center gap-3">
            <a
              href="#events"
              className="group inline-flex items-center gap-2 rounded-full bg-cosmo py-1.5 pl-5 pr-1.5 text-sm font-semibold text-white shadow-[3px_3px_0_var(--color-ink)] transition-transform duration-300 ease-[cubic-bezier(0.16,1,0.3,1)] hover:-translate-y-0.5"
            >
              {t.exploreEvents}
              <span className="flex h-8 w-8 items-center justify-center rounded-full bg-ink text-white transition-transform duration-300 group-hover:translate-x-0.5">
                <svg className="h-3.5 w-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2">
                  <path d="M5 12h14m-6-6 6 6-6 6" strokeLinecap="round" strokeLinejoin="round" />
                </svg>
              </span>
            </a>
            <Link
              href="/map"
              className="rounded-full border-[1.5px] border-ink bg-card px-5 py-2.5 text-sm font-semibold text-ink transition-transform duration-200 hover:-translate-y-0.5"
            >
              {t.viewMap}
            </Link>
          </div>
        </div>
      </section>

      {/* ── EVENTS — side filter + magazine grid ─────────────────────────── */}
      <section id="events" className="mx-auto max-w-6xl scroll-mt-24 px-4 pb-24 pt-6 md:px-6">
        <div className="lg:grid lg:grid-cols-[264px_1fr] lg:gap-8">
          <Suspense>
            <Filters categories={categories} />
          </Suspense>
          <div>
            <Suspense
              key={JSON.stringify(sp)}
              fallback={
                <div className="grid grid-cols-1 gap-5 sm:grid-cols-2 xl:grid-cols-3">
                  {Array.from({ length: 6 }).map((_, i) => (
                    <div
                      key={i}
                      className="aspect-[4/3] animate-pulse rounded-[1.5rem] border-[1.5px] border-line bg-card"
                    />
                  ))}
                </div>
              }
            >
              <EventGrid searchParams={sp} />
            </Suspense>
          </div>
        </div>
      </section>
    </>
  );
}
