import { Suspense } from "react";
import { cookies } from "next/headers";
import { fetchCategories, fetchMappableEvents } from "@/lib/events";
import { getDict } from "@/lib/i18n";
import type { Lang } from "@/lib/types";
import { Filters } from "@/components/filters";
import { MapShell } from "@/components/map-shell";

export const dynamic = "force-dynamic";

type Search = { [key: string]: string | string[] | undefined };

const one = (v: string | string[] | undefined) => (Array.isArray(v) ? v[0] : v);
function many(v: string | string[] | undefined): string[] {
  const s = Array.isArray(v) ? v.join(",") : (v ?? "");
  return s.split(",").map((x) => x.trim()).filter(Boolean);
}

/* Map + result count live in their own Suspense boundary keyed on the search
   params, so changing a filter re-streams only this part while the filter rail
   stays interactive. */
async function MapPanel({ searchParams }: { searchParams: Search }) {
  const cookieStore = await cookies();
  const lang = (cookieStore.get("lang")?.value === "es" ? "es" : "en") as Lang;
  const t = getDict(lang);

  const events = await fetchMappableEvents({
    q: one(searchParams.q),
    city: one(searchParams.city),
    when: one(searchParams.when),
    categories: many(searchParams.categories),
  }).catch((err) => {
    console.error("MapPanel:", err);
    return [];
  });

  return (
    <div>
      <p className="mb-5 font-condensed text-[13px] font-medium uppercase tracking-[0.16em] text-ink-soft">
        {events.length} {t.eventsOnMap}
      </p>

      {/* double-bezel map card */}
      <div className="rounded-[1.5rem] border-[1.5px] border-ink bg-card p-1.5 shadow-[4px_5px_0_var(--color-ink)]">
        <div className="h-[62dvh] min-h-[380px] overflow-hidden rounded-[1.05rem]">
          <MapShell events={events} />
        </div>
      </div>

      {events.length === 0 && (
        <p className="mt-5 rounded-[1.5rem] border-[1.5px] border-dashed border-ink/40 bg-card p-8 text-center text-ink-soft">
          {t.noEvents}
        </p>
      )}
    </div>
  );
}

export default async function MapPage({ searchParams }: { searchParams: Promise<Search> }) {
  const sp = await searchParams;
  const cookieStore = await cookies();
  const lang = (cookieStore.get("lang")?.value === "es" ? "es" : "en") as Lang;
  const t = getDict(lang);
  const categories = await fetchCategories().catch(() => []);

  return (
    <div className="mx-auto max-w-[96rem] px-4 pb-24 pt-10 md:px-6 md:pt-14 2xl:px-10">
      <div className="mb-6">
        <h1 className="font-display text-4xl font-black italic tracking-tight text-ink md:text-5xl">
          {t.map}
        </h1>
        <p className="mt-1.5 text-sm text-ink-soft">{t.mapIntro}</p>
      </div>

      <div className="lg:grid lg:grid-cols-[300px_minmax(0,1fr)] lg:gap-8 xl:grid-cols-[320px_minmax(0,1fr)] 2xl:grid-cols-[340px_minmax(0,1fr)] 2xl:gap-10">
        <Suspense>
          <Filters categories={categories} />
        </Suspense>
        <Suspense
          key={JSON.stringify(sp)}
          fallback={
            <div>
              <div className="mb-5 h-4 w-40 animate-pulse rounded-full bg-line" />
              <div className="h-[62dvh] min-h-[380px] animate-pulse rounded-[1.5rem] border-[1.5px] border-line bg-card" />
            </div>
          }
        >
          <MapPanel searchParams={sp} />
        </Suspense>
      </div>
    </div>
  );
}
