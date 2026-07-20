"use client";

import { useMemo } from "react";
import Link from "next/link";
import { MapContainer, TileLayer, Marker, Popup } from "react-leaflet";
import L from "leaflet";
import type { EventRow } from "@/lib/types";
import { useLang } from "./lang-context";
import { dateLocale } from "@/lib/i18n";

/* One pin per venue; events grouped under it. */
type VenuePin = {
  key: string;
  lat: number;
  lng: number;
  name: string | null;
  city: string | null;
  events: EventRow[];
};

const BORDER_CENTER: [number, number] = [31.72, -106.46]; // between El Paso + Juárez

function dot(count: number) {
  const size = count > 1 ? 30 : 22;
  return L.divIcon({
    className: "venue-dot",
    iconSize: [size, size],
    iconAnchor: [size / 2, size / 2],
    html: `<div style="
      width:${size}px;height:${size}px;border-radius:9999px;
      background:#f4652e;color:#0e0c0a;
      display:flex;align-items:center;justify-content:center;
      font:600 11px system-ui;border:2px solid #0e0c0a;
      box-shadow:0 0 0 2px rgba(244,101,46,.35), 0 4px 14px rgba(0,0,0,.5);
    ">${count > 1 ? count : ""}</div>`,
  });
}

export function EventMap({ events }: { events: EventRow[] }) {
  const { lang, t } = useLang();
  const locale = dateLocale(lang);

  const pins = useMemo(() => {
    const byVenue = new Map<string, VenuePin>();
    for (const e of events) {
      const v = e.venues;
      if (!v || v.lat == null || v.lng == null) continue;
      const key = v.id;
      const pin = byVenue.get(key) ?? {
        key,
        lat: v.lat,
        lng: v.lng,
        name: v.name,
        city: v.city,
        events: [],
      };
      pin.events.push(e);
      byVenue.set(key, pin);
    }
    return [...byVenue.values()];
  }, [events]);

  return (
    <MapContainer
      center={BORDER_CENTER}
      zoom={11}
      scrollWheelZoom
      className="h-full w-full"
      attributionControl
    >
      <TileLayer
        attribution='&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> &copy; <a href="https://carto.com/attributions">CARTO</a>'
        url="https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png"
      />
      {pins.map((pin) => (
        <Marker key={pin.key} position={[pin.lat, pin.lng]} icon={dot(pin.events.length)}>
          <Popup maxWidth={280}>
            <div style={{ minWidth: 200 }}>
              <p style={{ fontWeight: 600, fontSize: 13, marginBottom: 2 }}>
                {pin.name ?? pin.events[0]?.venue ?? ""}
              </p>
              {pin.city && (
                <p style={{ fontSize: 11, opacity: 0.6, marginBottom: 8 }}>{pin.city}</p>
              )}
              <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                {pin.events.slice(0, 4).map((e) => (
                  <Link
                    key={e.id}
                    href={`/events/${e.id}`}
                    style={{ fontSize: 12.5, lineHeight: 1.35, color: "#ff8a50" }}
                  >
                    {e.start_time
                      ? new Date(e.start_time).toLocaleDateString(locale, {
                          month: "short",
                          day: "numeric",
                        }) + " · "
                      : ""}
                    {e.title}
                  </Link>
                ))}
                {pin.events.length > 4 && (
                  <span style={{ fontSize: 11, opacity: 0.6 }}>
                    +{pin.events.length - 4} {t.eventsFound}
                  </span>
                )}
              </div>
            </div>
          </Popup>
        </Marker>
      ))}
    </MapContainer>
  );
}
