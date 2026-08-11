import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  reactStrictMode: true,

  // Next 16 blocks its own dev resources (client chunks, HMR) when the page is
  // opened from a host it does not consider same-origin. The app is reached at
  // both localhost:3000 and 127.0.0.1:3000, so allow both -- otherwise the page
  // renders but never hydrates when opened via 127.0.0.1.
  allowedDevOrigins: ["localhost", "127.0.0.1"],

  // The screen has no server-only Next features. Export plain HTML/CSS/JS so the
  // combined FastAPI app (local) or nginx (AWS) can serve it without a Node runtime.
  output: "export",
};

export default nextConfig;
