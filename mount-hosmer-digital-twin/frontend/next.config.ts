import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  reactStrictMode: true,

  // Emit .next/standalone: a self-contained server.js plus only the node_modules it
  // actually traced. The Docker image copies that instead of the full dependency
  // tree. This is additive -- `next dev` and `next start` are unaffected, so the
  // Windows/PowerShell route and the one-click .exe keep working unchanged.
  output: "standalone",
};

export default nextConfig;
