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
