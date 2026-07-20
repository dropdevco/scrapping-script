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
          <h1 className="font-display text-3xl font-bold tracking-tight text-sand md:text-4xl">
            {t.map}
          </h1>
          <p className="mt-1.5 text-sm text-sand-dim">{t.mapIntro}</p>
        </div>
        <p className="text-[13px] text-sand-faint">
          {events.length} {t.eventsOnMap}
        </p>
      </div>

      {/* double-bezel map card */}
      <div className="rounded-[1.75rem] bg-surface p-1.5 ring-1 ring-line/70">
        <div className="h-[62dvh] min-h-[380px] overflow-hidden rounded-[1.375rem]">
          <MapShell events={events} />
        </div>
      </div>
    </div>
  );
}
