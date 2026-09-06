"use client";

import { useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { APIProvider, Map as GoogleMap, Marker, InfoWindow } from "@vis.gl/react-google-maps";
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

const BORDER_CENTER = { lat: 31.72, lng: -106.46 }; // between El Paso + Juárez

/* Covers El Paso, Juárez, and nearby towns (Las Cruces, Alamogordo) that
   legitimately show up in the data. `strictBounds` stops both panning and
   zooming out past this box — a handful of venues have bad geocodes clear
   across Mexico (Mexico City, Guadalajara), and without a hard limit the
   map would happily scroll a visitor there. */
const REGION_BOUNDS = { north: 32.4, south: 31.35, east: -105.7, west: -106.9 };

const GOOGLE_MAPS_API_KEY = process.env.NEXT_PUBLIC_GOOGLE_MAPS_API_KEY ?? "";

/* Warm-cream palette matching the site's paper/ink/cosmo tokens — Google's
   default blue/green basemap clashes with the rest of the page otherwise. */
const MAP_STYLE: google.maps.MapTypeStyle[] = [
  { elementType: "geometry", stylers: [{ color: "#f3ebda" }] },
  { elementType: "labels.text.fill", stylers: [{ color: "#4a4550" }] },
  { elementType: "labels.text.stroke", stylers: [{ color: "#fffcf5" }] },
  { featureType: "poi", elementType: "labels", stylers: [{ visibility: "off" }] },
  { featureType: "poi.business", stylers: [{ visibility: "off" }] },
  { featureType: "poi.park", elementType: "geometry", stylers: [{ color: "#e6e2c8" }] },
  { featureType: "transit", stylers: [{ visibility: "off" }] },
  { featureType: "road", elementType: "geometry", stylers: [{ color: "#fffcf5" }] },
  { featureType: "road", elementType: "labels.icon", stylers: [{ visibility: "off" }] },
  { featureType: "road.arterial", elementType: "geometry", stylers: [{ color: "#fbf6ec" }] },
  { featureType: "road.highway", elementType: "geometry", stylers: [{ color: "#f0d9e4" }] },
  { featureType: "administrative", elementType: "geometry.stroke", stylers: [{ color: "#d8d0c0" }] },
  { featureType: "water", elementType: "geometry", stylers: [{ color: "#dbe2df" }] },
];

/* Plain {width,height}/{x,y} objects rather than `new google.maps.Size(...)`
   — markers can render before the Maps script (and the `google` global)
   has finished loading, and the API only ever reads these as data anyway. */
function dotIcon(count: number): google.maps.Icon {
  const size = count > 1 ? 30 : 22;
  const r = size / 2 - 1.5;
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="${size}" height="${size}">
    <circle cx="${size / 2}" cy="${size / 2}" r="${r}" fill="#e6117f" stroke="#141118" stroke-width="2" />
  </svg>`;
  return {
    url: `data:image/svg+xml;charset=UTF-8,${encodeURIComponent(svg)}`,
    scaledSize: { width: size, height: size } as google.maps.Size,
    anchor: { x: size / 2, y: size / 2 } as google.maps.Point,
    labelOrigin: { x: size / 2, y: size / 2 } as google.maps.Point,
  };
}

/* Touch devices need two fingers to pan (one-finger swipes are left for
   scrolling the page — the map card is 62dvh, most of a phone screen).
   Google's own "cooperative" gesture handling does this natively, complete
   with its own translated hint bubble, so it needs no custom gate like
   Leaflet did. Pointer devices stay "greedy": free drag + scroll-to-zoom. */
function useGestureHandling(): "cooperative" | "greedy" {
  const [coarse, setCoarse] = useState(() => window.matchMedia("(pointer: coarse)").matches);
  useEffect(() => {
    const mq = window.matchMedia("(pointer: coarse)");
    const onChange = () => setCoarse(mq.matches);
    mq.addEventListener("change", onChange);
    return () => mq.removeEventListener("change", onChange);
  }, []);
  return coarse ? "cooperative" : "greedy";
}

export function EventMap({ events }: { events: EventRow[] }) {
  const { lang, t } = useLang();
  const locale = dateLocale(lang);
  const gestureHandling = useGestureHandling();
  const [selectedKey, setSelectedKey] = useState<string | null>(null);

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

  const selectedPin = pins.find((p) => p.key === selectedKey) ?? null;

  if (!GOOGLE_MAPS_API_KEY) {
    return (
      <div className="flex h-full w-full items-center justify-center bg-paper-2 px-6 text-center text-sm text-ink-soft">
        Missing NEXT_PUBLIC_GOOGLE_MAPS_API_KEY
      </div>
    );
  }

  return (
    <APIProvider apiKey={GOOGLE_MAPS_API_KEY} language={lang === "es" ? "es" : "en"}>
      <GoogleMap
        defaultCenter={BORDER_CENTER}
        defaultZoom={11}
        restriction={{ latLngBounds: REGION_BOUNDS, strictBounds: true }}
        gestureHandling={gestureHandling}
        disableDefaultUI={false}
        streetViewControl={false}
        mapTypeControl={false}
        clickableIcons={false}
        styles={MAP_STYLE}
        onClick={() => setSelectedKey(null)}
        style={{ width: "100%", height: "100%" }}
      >
        {pins.map((pin) => (
          <Marker
            key={pin.key}
            position={{ lat: pin.lat, lng: pin.lng }}
            icon={dotIcon(pin.events.length)}
            label={
              pin.events.length > 1
                ? { text: String(pin.events.length), color: "#ffffff", fontSize: "11px", fontWeight: "700" }
                : undefined
            }
            onClick={() => setSelectedKey(pin.key)}
          />
        ))}

        {selectedPin && (
          <InfoWindow
            position={{ lat: selectedPin.lat, lng: selectedPin.lng }}
            maxWidth={280}
            onCloseClick={() => setSelectedKey(null)}
          >
            <div style={{ minWidth: 200 }}>
              <p style={{ fontWeight: 600, fontSize: 13, marginBottom: 2 }}>
                {selectedPin.name ?? selectedPin.events[0]?.venue ?? ""}
              </p>
              {selectedPin.city && (
                <p style={{ fontSize: 11, opacity: 0.6, marginBottom: 8 }}>{selectedPin.city}</p>
              )}
              <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                {selectedPin.events.slice(0, 4).map((e) => (
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
                {selectedPin.events.length > 4 && (
                  <span style={{ fontSize: 11, opacity: 0.6 }}>
                    +{selectedPin.events.length - 4} {t.eventsFound}
                  </span>
                )}
              </div>
            </div>
          </InfoWindow>
        )}
      </GoogleMap>
    </APIProvider>
  );
}
