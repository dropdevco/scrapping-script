import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Keep metadata in the initial HTML for crawler/knowledge-base extraction.
  htmlLimitedBots: /.*/,

  async redirects() {
    return [
      // `/events` is the URL people guess (and the one event detail pages sit
      // under), but the listing itself has always lived on `/` in the
      // `#events` section — so the bare path 404'd. Alias it rather than
      // building a second listing page: two URLs serving the same grid would
      // split ranking signals and double the maintenance.
      //
      // Only the exact path matches, so `/events/<id>` still routes to
      // `app/events/[id]/page.tsx`. Query strings are carried over, which
      // makes `/events?q=music&city=...` land on the filtered home grid.
      {
        source: "/events",
        destination: "/#events",
        permanent: true,
      },
    ];
  },
};

export default nextConfig;
