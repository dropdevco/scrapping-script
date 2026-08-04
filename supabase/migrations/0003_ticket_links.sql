-- 0003_ticket_links.sql — support multiple ticketing links per event (ADDITIVE ONLY;
-- no drops, no deletes, no alterations of existing columns; safe on a live database).
--
-- Same real-world event is often scraped from more than one ticketing site
-- (Ticketmaster, Eventbrite, AXS, a venue's own calendar). Previously each such
-- scrape could end up as its own event row; the scraper now detects the same
-- real event (matching venue + day + title) and merges duplicates into ONE row,
-- keeping every source's purchase link here instead of one row per site.
--
-- Shape: [{ "source": "events_ticketmaster", "label": "Ticketmaster", "url": "..." }, ...]

alter table public.events
    add column if not exists ticket_links jsonb not null default '[]'::jsonb;
