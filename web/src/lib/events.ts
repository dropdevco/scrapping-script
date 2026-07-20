import { supabaseServer } from "./supabase/server";
import type { EventRow } from "./types";

export type EventFilters = {
  q?: string;
  city?: string;      // "el paso" | "juarez"
  when?: string;      // "today" | "weekend" | "week"
  category?: string;
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

export async function fetchEvents(filters: EventFilters, limit = 60): Promise<EventRow[]> {
  const supabase = await supabaseServer();
  let q = supabase
    .from("events")
    .select("*, venues(*)")
    .eq("status", "approved")
    .order("start_time", { ascending: true, nullsFirst: false })
    .limit(limit);

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

  if (filters.category) {
    q = q.contains("categories", [filters.category]);
  }

  const { data, error } = await q;
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
  const supabase = await supabaseServer();
  const { data } = await supabase
    .from("events")
    .select("categories")
    .eq("status", "approved")
    .gte("start_time", new Date().toISOString())
    .limit(500);
  const counts = new Map<string, number>();
  for (const row of data ?? []) {
    for (const c of (row.categories as string[] | null) ?? []) {
      counts.set(c, (counts.get(c) ?? 0) + 1);
    }
  }
  return [...counts.entries()]
    .sort((a, b) => b[1] - a[1])
    .slice(0, 8)
    .map(([c]) => c);
}

export async function fetchMappableEvents(limit = 200): Promise<EventRow[]> {
  const supabase = await supabaseServer();
  const { data, error } = await supabase
    .from("events")
    .select("*, venues!inner(*)")
    .eq("status", "approved")
    .gte("start_time", new Date().toISOString())
    .not("venues.lat", "is", null)
    .order("start_time", { ascending: true })
    .limit(limit);
  if (error) throw new Error(`map query failed: ${error.message}`);
  return (data ?? []) as EventRow[];
}
