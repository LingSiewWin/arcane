import type { Metadata } from "next";
import { Geist, Geist_Mono } from "next/font/google";

import { TooltipProvider } from "@web/ui/components/tooltip";

import "../index.css";
import Header from "@/components/header";
import Providers from "@/components/providers";

const geistSans = Geist({
  variable: "--font-geist-sans",
  subsets: ["latin"],
});

const geistMono = Geist_Mono({
  variable: "--font-geist-mono",
  subsets: ["latin"],
});

export const metadata: Metadata = {
  title: "Agent Arena · Arc living economy",
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
      <body className={`${geistSans.variable} ${geistMono.variable} antialiased`}>
        <Providers>
          <TooltipProvider>
            <div className="grid h-svh grid-rows-[auto_1fr]">
              <Header />
              <div className="min-h-0 overflow-y-auto">{children}</div>
            </div>
          </TooltipProvider>
        </Providers>
      </body>
    </html>
  );
}
