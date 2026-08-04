import type { EventRow } from "./types";
import { absoluteUrl, eventUrl } from "./site";

function text(value: string | null | undefined): string | undefined {
  const clean = value?.replace(/\s+/g, " ").trim();
  return clean || undefined;
}

export function eventDescription(event: EventRow): string {
  return (
    text(event.description) ??
    [
      event.title,
      event.venues?.name ?? event.venue,
      event.venues?.city,
      event.start_time ? new Date(event.start_time).toLocaleString("en-US") : undefined,
    ]
      .filter(Boolean)
      .join(" - ")
  );
}

export function eventImageUrl(event: EventRow): string | undefined {
  return absoluteUrl(event.image_url);
}

export function buildEventJsonLd(event: EventRow) {
  const venue = event.venues;
  const venueName = text(venue?.name) ?? text(event.venue);
  const address = text(venue?.address) ?? text(event.location);
  const image = eventImageUrl(event);
  const url = eventUrl(event.id);
  const ticketUrl = absoluteUrl(event.url);
  const ticketLinks = (event.ticket_links ?? []).filter((l) => l.url);
  const hasPhysicalLocation = Boolean(address || venueName);

  const postalAddress = address || venue?.city || venue?.region || venue?.postal || venue?.country
    ? {
        "@type": "PostalAddress",
        streetAddress: address,
        addressLocality: text(venue?.city),
        addressRegion: text(venue?.region),
        postalCode: text(venue?.postal),
        addressCountry: text(venue?.country),
      }
    : undefined;

  const location = hasPhysicalLocation
    ? {
        "@type": "Place",
        name: venueName ?? address,
        address: postalAddress,
        geo:
          venue?.lat != null && venue?.lng != null
            ? {
                "@type": "GeoCoordinates",
                latitude: venue.lat,
                longitude: venue.lng,
              }
            : undefined,
      }
    : {
        "@type": "VirtualLocation",
        url: ticketUrl ?? url,
      };

  return {
    "@context": "https://schema.org",
    "@type": "Event",
    "@id": `${url}#event`,
    name: event.title,
    description: eventDescription(event),
    startDate: event.start_time ?? undefined,
    endDate: event.end_time ?? undefined,
    eventStatus: "https://schema.org/EventScheduled",
    eventAttendanceMode: hasPhysicalLocation
      ? "https://schema.org/OfflineEventAttendanceMode"
      : "https://schema.org/OnlineEventAttendanceMode",
    location,
    image: image ? [image] : undefined,
    url,
    sameAs: ticketUrl && ticketUrl !== url ? ticketUrl : undefined,
    // schema.org/Event.offers accepts either one Offer or an array — reflect
    // every known ticketing source when there's more than one, falling back
    // to the single legacy `url` field for older/un-migrated rows.
    offers:
      ticketLinks.length > 0
        ? ticketLinks.map((link) => ({
            "@type": "Offer" as const,
            url: absoluteUrl(link.url),
            seller: { "@type": "Organization" as const, name: link.label },
            availability: "https://schema.org/InStock",
          }))
        : ticketUrl
          ? { "@type": "Offer" as const, url: ticketUrl, availability: "https://schema.org/InStock" }
          : undefined,
    category: event.categories?.filter(Boolean) ?? undefined,
    identifier: event.source_id ?? event.id,
    provider: {
      "@type": "Organization",
      name: event.source.replace("events_", ""),
    },
    mainEntityOfPage: url,
  };
}

export function jsonLdScript(data: unknown): string {
  return JSON.stringify(data).replace(/</g, "\\u003c");
}
