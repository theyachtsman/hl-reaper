import type { Metadata, Viewport } from "next";
import "@/styles/globals.css";
import Header from "@/components/Header";
import BottomNav from "@/components/BottomNav";

export const metadata: Metadata = {
  title: "HL Reaper",
  description: "Hyperliquid perp bot dashboard (LAN only)",
};

export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  themeColor: "#0b0f14",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>
        <Header />
        <main className="max-w-6xl mx-auto px-3 md:px-4 py-4 md:py-6 pb-24 md:pb-6">
          {children}
        </main>
        <BottomNav />
      </body>
    </html>
  );
}
