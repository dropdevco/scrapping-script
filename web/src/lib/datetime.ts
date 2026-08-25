/**
 * Event timestamps are always shown in El Paso's local time — never the
 * viewer's, and never the server's.
 *
 * Two things went wrong without this. Server-rendered pages format on a Vercel
 * box whose clock is UTC, so an 8pm show was printed as "2:00 AM" in the HTML
 * and then silently changed after hydration when the browser re-formatted it in
 * its own zone. And a visitor reading from another timezone got that show
 * translated into *their* local time, which is meaningless for deciding whether
 * to drive to a venue in El Paso. Pinning the zone makes server and client agree
 * and makes the printed time the one written on the door.
 */
export const EVENT_TZ = "America/Denver";

export function formatEventDate(
  iso: string | null | undefined,
  locale: string,
  options: Intl.DateTimeFormatOptions = { weekday: "short", month: "short", day: "numeric" },
): string | null {
  if (!iso) return null;
  return new Date(iso).toLocaleDateString(locale, { ...options, timeZone: EVENT_TZ });
}

export function formatEventTime(
  iso: string | null | undefined,
  locale: string,
  options: Intl.DateTimeFormatOptions = { hour: "numeric", minute: "2-digit" },
): string | null {
  if (!iso) return null;
  return new Date(iso).toLocaleTimeString(locale, { ...options, timeZone: EVENT_TZ });
}

export function formatEventDateTime(
  iso: string | null | undefined,
  locale: string,
  options: Intl.DateTimeFormatOptions = {},
): string | null {
  if (!iso) return null;
  return new Date(iso).toLocaleString(locale, { ...options, timeZone: EVENT_TZ });
}

/**
 * Turn a `<input type="datetime-local">` value ("2026-08-26T20:00") into an
 * absolute ISO instant, reading it as El Paso wall-clock time.
 *
 * `new Date(naive)` reads the value in the *browser's* zone, so a submitter
 * travelling — or simply on a laptop still set to another city — filed an 8pm
 * show at 8pm their time, which is the same class of bug the scraper had.
 * Nobody submitting to a bilingual El Paso/Juárez events board means anything
 * other than local time when they type 8:00 PM.
 */
export function eventLocalToIso(naive: string): string {
  const asIfUtc = new Date(`${naive}Z`);
  if (Number.isNaN(asIfUtc.getTime())) return new Date(naive).toISOString();

  const offsetAt = (instant: Date): number => {
    const parts = new Intl.DateTimeFormat("en-US", {
      timeZone: EVENT_TZ,
      hourCycle: "h23",
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    })
      .formatToParts(instant)
      .reduce<Record<string, number>>((acc, p) => {
        if (p.type !== "literal") acc[p.type] = Number(p.value);
        return acc;
      }, {});
    const wallClock = Date.UTC(
      parts.year,
      parts.month - 1,
      parts.day,
      parts.hour,
      parts.minute,
      parts.second,
    );
    return wallClock - instant.getTime();
  };

  // Two passes: the first offset is sampled at the wrong instant on the two days
  // a year the offset changes, so it is re-sampled at the corrected one.
  const firstPass = asIfUtc.getTime() - offsetAt(asIfUtc);
  return new Date(asIfUtc.getTime() - offsetAt(new Date(firstPass))).toISOString();
}
