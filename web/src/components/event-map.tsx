"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";
import { MapContainer, TileLayer, Marker, Popup, useMap } from "react-leaflet";
import L from "leaflet";
import type { EventRow } from "@/lib/types";
import { useLang } from "./lang-context";
import { formatEventDate } from "@/lib/datetime";
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
      background:#e6117f;color:#ffffff;
      display:flex;align-items:center;justify-content:center;
      font:700 11px system-ui;border:2px solid #141118;
      box-shadow:2px 2px 0 rgba(20,17,24,.9);
    ">${count > 1 ? count : ""}</div>`,
  });
}

/*
  Two-finger panning on touch devices.

  The map card is 62dvh — on a phone that is most of the screen, so with
  Leaflet's default one-finger dragging there is almost nowhere left to put
  your thumb to scroll the PAGE: every swipe gets eaten by the map and the
  user is stranded. Same reasoning as the hero scratch surface.

  So on coarse pointers we hand one-finger swipes back to the page and only
  enable dragging while two fingers are down (the convention embedded maps
  use), with a brief hint the first time a one-finger drag is swallowed.
  Pointer devices are untouched — dragging stays on for the mouse.
*/
function TouchPanGate({ hint }: { hint: string }) {
  const map = useMap();
  const [nudge, setNudge] = useState(false);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    if (!window.matchMedia("(pointer: coarse)").matches) return;
    const container = map.getContainer();
    map.dragging.disable();

    const showNudge = () => {
      setNudge(true);
      if (timer.current) clearTimeout(timer.current);
      timer.current = setTimeout(() => setNudge(false), 1600);
    };

    const onTouchStart = (e: TouchEvent) => {
      if (e.touches.length >= 2) {
        map.dragging.enable();
        setNudge(false);
        if (timer.current) clearTimeout(timer.current);
      } else {
        map.dragging.disable();
      }
    };
    const onTouchMove = (e: TouchEvent) => {
      if (e.touches.length < 2) showNudge();
    };
    const onTouchEnd = (e: TouchEvent) => {
      if (e.touches.length < 2) map.dragging.disable();
    };

    container.addEventListener("touchstart", onTouchStart, { passive: true });
    container.addEventListener("touchmove", onTouchMove, { passive: true });
    container.addEventListener("touchend", onTouchEnd, { passive: true });
    container.addEventListener("touchcancel", onTouchEnd, { passive: true });

    return () => {
      if (timer.current) clearTimeout(timer.current);
      container.removeEventListener("touchstart", onTouchStart);
      container.removeEventListener("touchmove", onTouchMove);
      container.removeEventListener("touchend", onTouchEnd);
      container.removeEventListener("touchcancel", onTouchEnd);
    };
  }, [map]);

  if (!nudge) return null;

  return (
    <div className="pointer-events-none absolute inset-0 z-[500] flex items-center justify-center">
      <span className="rounded-full border-[1.5px] border-ink bg-card/95 px-4 py-2 font-condensed text-[12px] font-semibold uppercase tracking-[0.14em] text-ink shadow-[2px_2px_0_var(--color-ink)]">
        {hint}
      </span>
    </div>
  );
}

export function EventMap({ events }: { events: EventRow[] }) {
  const { lang, t } = useLang();
  const locale = dateLocale(lang);

  const pins = useMemo(() => {
    // Grouped by COORDINATE, not venue id: the same physical place often has
    // several venue rows (different address spellings hash to different
    // venues), which would otherwise stack identical pins on one spot.
    const bySpot = new Map<string, VenuePin>();
    for (const e of events) {
      const v = e.venues;
      if (!v || v.lat == null || v.lng == null) continue;
      const key = `${v.lat.toFixed(5)},${v.lng.toFixed(5)}`;
      const pin = bySpot.get(key) ?? {
        key,
        lat: v.lat,
        lng: v.lng,
        name: v.name,
        city: v.city,
        events: [],
      };
      pin.events.push(e);
      bySpot.set(key, pin);
    }
    return [...bySpot.values()];
  }, [events]);

  return (
    <MapContainer
      center={BORDER_CENTER}
      zoom={11}
      scrollWheelZoom
      className="h-full w-full"
      attributionControl
    >
      <TouchPanGate hint={t.mapTwoFinger} />
      <TileLayer
        attribution='&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> &copy; <a href="https://carto.com/attributions">CARTO</a>'
        url="https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png"
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
                    style={{ fontSize: 12.5, lineHeight: 1.35, color: "#e6117f", fontWeight: 600 }}
                  >
                    {e.start_time
                      ? formatEventDate(e.start_time, locale, {
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
