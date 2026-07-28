import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Keep metadata in the initial HTML for crawler/knowledge-base extraction.
  htmlLimitedBots: /.*/,
};

export default nextConfig;
