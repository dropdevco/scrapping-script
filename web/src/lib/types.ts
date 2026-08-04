export type Venue = {
  id: string;
  name: string | null;
  address: string | null;
  city: string | null;
  region: string | null;
  postal: string | null;
  country: string | null;
  lat: number | null;
  lng: number | null;
};

export type TicketLink = {
  source: string;
  label: string; // human label, e.g. "Ticketmaster", "Eventbrite"
  url: string;
};

export type EventRow = {
  id: string;
  source: string;
  source_id: string | null;
  title: string;
  description: string | null;
  start_time: string | null;
  end_time: string | null;
  venue: string | null;
  location: string | null;
  url: string | null;
  image_url: string | null;
  categories: string[] | null;
  // Multiple places to buy tickets for the SAME real event — the scraper merges
  // duplicate listings from different ticketing sites into one row and collects
  // every source's link here instead of showing one card per site. Older rows
  // (scraped before this existed) or user submissions may have an empty array;
  // fall back to `url` in that case.
  ticket_links: TicketLink[] | null;
  status: string;
  venue_id: string | null;
  venues: Venue | null; // joined venue row
  first_seen?: string | null;
  last_seen?: string | null;
};

export type Lang = "en" | "es";
