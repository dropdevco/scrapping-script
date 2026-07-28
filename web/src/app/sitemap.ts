import type { MetadataRoute } from "next";
import { fetchCrawlerEvents } from "@/lib/events";
import { eventUrl, siteUrl } from "@/lib/site";

export const dynamic = "force-dynamic";

export default async function sitemap(): Promise<MetadataRoute.Sitemap> {
  const events = await fetchCrawlerEvents().catch(() => []);
  const staticPages: MetadataRoute.Sitemap = [
    {
      url: siteUrl("/"),
      lastModified: new Date(),
      changeFrequency: "daily",
      priority: 1,
    },
    {
      url: siteUrl("/map"),
      lastModified: new Date(),
      changeFrequency: "daily",
      priority: 0.7,
    },
    {
      url: siteUrl("/crawler/events"),
      lastModified: new Date(),
      changeFrequency: "daily",
      priority: 0.9,
    },
  ];

  return [
    ...staticPages,
    ...events.map((event) => ({
      url: eventUrl(event.id),
      lastModified: event.last_seen ?? event.first_seen ?? event.start_time ?? new Date(),
      changeFrequency: "daily" as const,
      priority: 0.8,
    })),
  ];
}
