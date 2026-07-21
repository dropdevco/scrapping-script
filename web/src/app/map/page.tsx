import { cookies } from "next/headers";
import { fetchMappableEvents } from "@/lib/events";
import { getDict } from "@/lib/i18n";
import type { Lang } from "@/lib/types";
import { MapShell } from "@/components/map-shell";

export const dynamic = "force-dynamic";

export default async function MapPage() {
  const cookieStore = await cookies();
  const lang = (cookieStore.get("lang")?.value === "es" ? "es" : "en") as Lang;
  const t = getDict(lang);
  const events = await fetchMappableEvents().catch(() => []);

  return (
    <div className="mx-auto max-w-6xl px-4 pb-24 pt-10 md:px-6 md:pt-14">
      <div className="mb-6 flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="font-display text-4xl font-black italic tracking-tight text-ink md:text-5xl">
            {t.map}
          </h1>
          <p className="mt-1.5 text-sm text-ink-soft">{t.mapIntro}</p>
        </div>
        <p className="font-condensed text-[13px] font-medium uppercase tracking-[0.16em] text-ink-soft">
          {events.length} {t.eventsOnMap}
        </p>
      </div>

      {/* double-bezel map card */}
      <div className="rounded-[1.5rem] border-[1.5px] border-ink bg-card p-1.5 shadow-[4px_5px_0_var(--color-ink)]">
        <div className="h-[62dvh] min-h-[380px] overflow-hidden rounded-[1.05rem]">
          <MapShell events={events} />
        </div>
      </div>
    </div>
  );
}
