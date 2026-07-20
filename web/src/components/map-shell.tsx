"use client";

import dynamic from "next/dynamic";
import type { EventRow } from "@/lib/types";

/* Leaflet touches `window` — must never render on the server. */
const EventMap = dynamic(() => import("./event-map").then((m) => m.EventMap), {
  ssr: false,
  loading: () => (
    <div className="flex h-full w-full items-center justify-center bg-surface">
      <span className="h-2 w-2 animate-ping rounded-full bg-sunset" />
    </div>
  ),
});

export function MapShell({ events }: { events: EventRow[] }) {
  return <EventMap events={events} />;
}
