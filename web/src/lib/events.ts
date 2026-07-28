import { supabaseServer } from "./supabase/server";
import type { EventRow } from "./types";
import { CANONICAL_CATEGORIES, expandedCategoryAliases } from "./categories";

export type EventFilters = {
  q?: string;
  city?: string;        // "el paso" | "juarez"
  when?: string;        // "today" | "weekend" | "week"
  categories?: string[]; // multi-select — OR semantics
};

/* Juárez appears in data as "Juárez", "Ciudad Juárez", unaccented "Juarez"… */
const CITY_PATTERNS: Record<string, string[]> = {
  "el paso": ["El Paso"],
  juarez: ["Juárez", "Juarez"],
};

function range(when: string | undefined): { from?: Date; to?: Date } {
  const now = new Date();
  const startOfDay = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  if (when === "today") {
    const to = new Date(startOfDay);
    to.setDate(to.getDate() + 1);
    return { from: now, to };
  }
  if (when === "weekend") {
    // Upcoming Fri 00:00 → Mon 00:00 (or the current weekend if we're in it)
    const day = startOfDay.getDay(); // 0 Sun … 6 Sat
    const daysToFriday = day <= 5 ? 5 - day : 6;
    const from = new Date(startOfDay);
    from.setDate(from.getDate() + (day === 6 || day === 0 ? 0 : daysToFriday));
    if (day === 0) from.setDate(from.getDate() - 2); // Sunday: weekend started Friday
    const to = new Date(from);
    to.setDate(to.getDate() + 3);
    return { from: day === 6 || day === 0 ? now : from, to };
  }
  if (when === "week") {
    const to = new Date(startOfDay);
    to.setDate(to.getDate() + 8);
    return { from: now, to };
  }
  return { from: now }; // default: anything upcoming
}

/* Shared by the list view and the map view so both surfaces interpret the
   same URL params identically. `q` is a PostgrestFilterBuilder. */
function applyEventFilters<T>(query: T, filters: EventFilters): T {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  let q = query as any;

  const { from, to } = range(filters.when);
  if (from) q = q.gte("start_time", from.toISOString());
  if (to) q = q.lte("start_time", to.toISOString());

  if (filters.q) {
    const term = filters.q.replaceAll(",", " ").trim();
    if (term) q = q.or(`title.ilike.%${term}%,venue.ilike.%${term}%,description.ilike.%${term}%`);
  }

  if (filters.city && CITY_PATTERNS[filters.city]) {
    const pats = CITY_PATTERNS[filters.city].map((p) => `location.ilike.%${p}%`);
    q = q.or(pats.join(","));
  }

  if (filters.categories && filters.categories.length > 0) {
    // OR semantics: event matches if it carries ANY selected category.
    q = q.overlaps("categories", expandedCategoryAliases(filters.categories));
  }

  return q as T;
}

export async function fetchEvents(filters: EventFilters, limit = 500): Promise<EventRow[]> {
  const supabase = await supabaseServer();
  const base = supabase
    .from("events")
    .select("*, venues(*)")
    .eq("status", "approved")
    .order("start_time", { ascending: true, nullsFirst: false })
    .limit(limit);

  const { data, error } = await applyEventFilters(base, filters);
  if (error) throw new Error(`events query failed: ${error.message}`);
  return (data ?? []) as EventRow[];
}

export async function fetchEvent(id: string): Promise<EventRow | null> {
  const supabase = await supabaseServer();
  const { data, error } = await supabase
    .from("events")
    .select("*, venues(*)")
    .eq("id", id)
    .maybeSingle();
  if (error) throw new Error(`event query failed: ${error.message}`);
  return (data as EventRow) ?? null;
}

export async function fetchCategories(): Promise<string[]> {
  return CANONICAL_CATEGORIES;
}

/* Same filter contract as fetchEvents, additionally restricted to events whose
   venue carries coordinates. `range()` inside applyEventFilters supplies the
   upcoming-only lower bound, so no separate start_time clause is needed. */
export async function fetchMappableEvents(
  filters: EventFilters = {},
  limit = 500,
): Promise<EventRow[]> {
  const supabase = await supabaseServer();
  const base = supabase
    .from("events")
    .select("*, venues!inner(*)")
    .eq("status", "approved")
    .not("venues.lat", "is", null)
    .order("start_time", { ascending: true })
    .limit(limit);

  const { data, error } = await applyEventFilters(base, filters);
  if (error) throw new Error(`map query failed: ${error.message}`);
  return (data ?? []) as EventRow[];
}

export async function fetchCrawlerEvents(limit = 500): Promise<EventRow[]> {
  const supabase = await supabaseServer();
  const { data, error } = await supabase
    .from("events")
    .select("*, venues(*)")
    .eq("status", "approved")
    .gte("start_time", new Date().toISOString())
    .order("start_time", { ascending: true, nullsFirst: false })
    .limit(limit);
  if (error) throw new Error(`crawler events query failed: ${error.message}`);
  return (data ?? []) as EventRow[];
}
