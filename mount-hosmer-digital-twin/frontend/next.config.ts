import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  reactStrictMode: true,

  // Next 16 blocks its own dev resources (client chunks, HMR) when the page is
  // opened from a host it does not consider same-origin. The app is reached at
  // both localhost:3000 and 127.0.0.1:3000, so allow both -- otherwise the page
  // renders but never hydrates when opened via 127.0.0.1.
  allowedDevOrigins: ["localhost", "127.0.0.1"],

  // Emit .next/standalone: a self-contained server.js plus only the node_modules it
  // actually traced. The Docker image copies that instead of the full dependency
  // tree. This is additive -- `next dev` and `next start` are unaffected, so the
  // Windows/PowerShell route and the one-click .exe keep working unchanged.
  output: "standalone",
};

export default nextConfig;
