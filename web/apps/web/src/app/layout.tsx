import type { Metadata } from "next";
import { Geist, Geist_Mono, Readex_Pro } from "next/font/google";

import "../index.css";
import Providers from "@/components/providers";

const geistSans = Geist({
  variable: "--font-geist-sans",
  subsets: ["latin"],
});

const geistMono = Geist_Mono({
  variable: "--font-geist-mono",
  subsets: ["latin"],
});

const readexPro = Readex_Pro({
  variable: "--font-readex-pro",
  weight: ["300", "400", "500", "600", "700"],
  subsets: ["latin"],
});

export const metadata: Metadata = {
  title: "Arcane · Arc living economy",
  description:
    "A living agentic economy on Arc testnet: agents register on-chain identities, publish and sell reasoning alpha, query each other, and are scored by a real oracle — with a live observatory + operator console. Read-only browser reads.",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en" suppressHydrationWarning>
      <body
        className={`${geistSans.variable} ${geistMono.variable} ${readexPro.variable} antialiased`}
      >
        <Providers>{children}</Providers>
      </body>
    </html>
  );
}
