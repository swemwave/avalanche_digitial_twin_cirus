import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Mount Hosmer Digital Twin",
  description: "Local research prototype for Mount Hosmer avalanche data discovery.",
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
