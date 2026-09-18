"use client";

/* SHA-1 hex via Web Crypto — used for venue address_hash and for the event
   content_hash uniqueness key. */
export async function sha1Hex(input: string): Promise<string> {
  const data = new TextEncoder().encode(input);
  const digest = await crypto.subtle.digest("SHA-1", data);
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

/* ── venue identity ──────────────────────────────────────────────────────────

   A LINE-FOR-LINE PORT of src/scraper/core/address.py. The submission form
   resolves an existing venue by this hash before creating one, so if these two
   implementations drift apart every user-submitted venue forks a second row —
   which is the exact bug the normalizer was written to fix (85 of 439 venue
   rows were redundant on 2026-09-17, which in turn defeated the event merge and
   put duplicate events on the carousel).

   Change one side, change the other, and re-check parity. */

const COUNTRY_TAIL = new Set(["us", "usa", "united states", "mx", "mex", "mexico"]);

// Applied to the FIRST token only — "One Civic Center Plaza" vs "1 Civic Center
// Plaza" is one theatre that held three venue ids. A mid-address number word
// ("Two Bridges Road") is a street name and is left alone.
const NUMBER_WORDS: Record<string, string> = {
  one: "1", two: "2", three: "3", four: "4", five: "5",
  six: "6", seven: "7", eight: "8", nine: "9", ten: "10",
};

const ABBREVIATIONS: Record<string, string> = {
  street: "st", str: "st",
  avenue: "ave", av: "ave",
  boulevard: "blvd",
  drive: "dr",
  road: "rd",
  place: "pl",
  court: "ct",
  lane: "ln",
  parkway: "pkwy",
  highway: "hwy",
  circle: "cir",
  north: "n", south: "s", east: "e", west: "w",
  northeast: "ne", northwest: "nw", southeast: "se", southwest: "sw",
  // Suite markers are canonicalized, never dropped: two tenants in one
  // building must stay two venues.
  suite: "ste", unit: "ste", apt: "ste", apartment: "ste",
};

function fold(text: string | null): string {
  const stripped = (text ?? "").normalize("NFKD").replace(/\p{M}/gu, "");
  let lowered = stripped.toLowerCase().split("#").join(" ste ");
  lowered = lowered.replace(/\b(\d{5})-\d{4}\b/g, "$1");
  return lowered.replace(/[^a-z0-9]+/g, " ").trim();
}

export function normalizeAddress(address: string | null): string {
  const tokens = fold(address).split(" ").filter(Boolean);
  if (tokens.length === 0) return "";

  if (tokens.length >= 2 && COUNTRY_TAIL.has(tokens.slice(-2).join(" "))) {
    tokens.splice(-2, 2);
  } else if (tokens.length > 0 && COUNTRY_TAIL.has(tokens[tokens.length - 1])) {
    tokens.pop();
  }

  if (tokens.length > 0 && NUMBER_WORDS[tokens[0]]) {
    tokens[0] = NUMBER_WORDS[tokens[0]];
  }

  return tokens.map((t) => ABBREVIATIONS[t] ?? t).join(" ");
}

export function normalizeVenueName(name: string | null): string {
  const folded = fold(name);
  return folded.startsWith("the ") ? folded.slice(4) : folded;
}

export async function venueAddressHash(address: string | null, venueName: string | null) {
  return sha1Hex(`${normalizeAddress(address)}|${normalizeVenueName(venueName)}`);
}
