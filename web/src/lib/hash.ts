"use client";

/* SHA-1 hex via Web Crypto — used for venue address_hash (must match the
   scraper rule: sha1(lower(trim(address)) + "|" + lower(trim(venue_name))))
   and for the event content_hash uniqueness key. */
export async function sha1Hex(input: string): Promise<string> {
  const data = new TextEncoder().encode(input);
  const digest = await crypto.subtle.digest("SHA-1", data);
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

export async function venueAddressHash(address: string | null, venueName: string | null) {
  const key = `${(address ?? "").trim().toLowerCase()}|${(venueName ?? "").trim().toLowerCase()}`;
  return sha1Hex(key);
}
