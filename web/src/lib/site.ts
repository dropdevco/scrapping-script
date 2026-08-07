const LOCAL_SITE_URL = "http://localhost:3000";

function normalizeOrigin(value: string | undefined): string {
  const raw = value?.trim();
  if (!raw) return LOCAL_SITE_URL;
  const withProtocol = raw.startsWith("http://") || raw.startsWith("https://")
    ? raw
    : `https://${raw}`;

  try {
    return new URL(withProtocol).origin;
  } catch {
    return LOCAL_SITE_URL;
  }
}

export function siteOrigin(): string {
  if (process.env.VERCEL_ENV === "production" && !process.env.NEXT_PUBLIC_SITE_URL) {
    // Falling back to VERCEL_URL here is silent and wrong in prod: it's
    // Vercel's internal per-deployment hostname, not the custom domain, so
    // every absolute URL the app emits (sitemap, robots.txt, canonical
    // links, JSON-LD, OG tags) ends up pointing at an unlisted Vercel host
    // instead of the real site. That exact bug shipped once already — a
    // crawler trying to resolve the robots.txt Sitemap: line got a
    // different origin than the one it was crawling and gave up.
    console.warn(
      "[site] NEXT_PUBLIC_SITE_URL is not set in production — falling back to " +
        "VERCEL_URL, which is the internal deployment hostname, not the custom " +
        "domain. Set NEXT_PUBLIC_SITE_URL in the Vercel project's Production " +
        "environment variables and redeploy.",
    );
  }
  return normalizeOrigin(process.env.NEXT_PUBLIC_SITE_URL ?? process.env.VERCEL_URL);
}

export function siteUrl(path = "/"): string {
  return new URL(path, siteOrigin()).toString();
}

export function eventPath(id: string): string {
  return `/events/${encodeURIComponent(id)}`;
}

export function eventUrl(id: string): string {
  return siteUrl(eventPath(id));
}

export function absoluteUrl(value: string | null | undefined): string | undefined {
  if (!value) return undefined;
  try {
    return new URL(value).toString();
  } catch {
    return siteUrl(value.startsWith("/") ? value : `/${value}`);
  }
}
