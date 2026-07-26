import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Mount Hosmer Digital Twin",
  description:
    "Experimental, non-operational avalanche terrain digital twin for Mount Hosmer, BC. Not a forecast.",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
