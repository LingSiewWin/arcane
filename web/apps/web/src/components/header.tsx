"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState } from "react";

import { useActiveDuel } from "@/lib/colosseum";

import { ConnectWallet } from "./connect-wallet";
import { ModeToggle } from "./mode-toggle";

const NAV = [
  { href: "/arena", label: "arena" },
  { href: "/colosseum", label: "colosseum" },
  { href: "/console", label: "console" },
] as const;

function isActive(pathname: string, href: string): boolean {
  if (href === "/") return pathname === "/";
  return pathname === href || pathname.startsWith(`${href}/`);
}

/**
 * Compact live-duel pill beside the brand. Reads the latest duel off-chain via
 * useActiveDuel (which wraps duelCount + getDuel). Live (pinging dot) when the
 * duel is in its on-chain trading window, muted once resolved or pre-trading.
 */
function LiveDuelPill() {
  const { data: duel } = useActiveDuel();
  const [nowSec, setNowSec] = useState(() => Math.floor(Date.now() / 1000));
  useEffect(() => {
    const id = setInterval(() => setNowSec(Math.floor(Date.now() / 1000)), 1_000);
    return () => clearInterval(id);
  }, []);

  if (!duel) return null;

  const trading =
    duel.status === 1 &&
    nowSec >= Number(duel.tradingStartsAt) &&
    nowSec < Number(duel.endsAt);

  return (
    <Link
      href="/colosseum"
      className={[
        "inline-flex items-center gap-1.5 rounded-full border px-2.5 py-0.5 font-mono text-[10px] transition-colors",
        trading
          ? "border-[--color-signal]/40 bg-[--color-signal]/10 text-[--color-signal] hover:bg-[--color-signal]/20"
          : "border-border/60 text-muted-foreground hover:bg-muted/50 hover:text-foreground",
      ].join(" ")}
    >
      <span className="relative flex size-1.5">
        {trading ? (
          <span className="absolute inline-flex size-full animate-ping rounded-full bg-[--color-signal] opacity-70" />
        ) : null}
        <span
          className={`relative inline-flex size-1.5 rounded-full ${
            trading ? "bg-[--color-signal]" : "bg-muted-foreground"
          }`}
        />
      </span>
      Duel #{duel.duelId} {trading ? "live" : "resolved"} · inject →
    </Link>
  );
}

export default function Header() {
  const pathname = usePathname() ?? "/";
  return (
    <header className="border-b border-border/60 bg-card/30 backdrop-blur">
      <div className="mx-auto flex w-full max-w-7xl items-center justify-between gap-4 px-4 py-2 sm:px-6">
        <div className="flex items-center gap-5">
          <Link
            href="/"
            className="flex items-center gap-2 rounded-full bg-neutral-900/90 py-1.5 pl-2.5 pr-4 backdrop-blur"
          >
            <svg viewBox="0 0 256 256" className="h-4 w-4" fill="#ffffff" xmlns="http://www.w3.org/2000/svg">
              <path d="M 128 192 L 128 256 L 64.5 256 L 32 223 L 0 192 L 0 128 L 64 128 Z M 256 192 L 256 256 L 192.5 256 L 160 223 L 128 192 L 128 128 L 192 128 Z M 128 64 L 128 128 L 64.5 128 L 32 95 L 0 64 L 0 0 L 64 0 Z M 256 64 L 256 128 L 192.5 128 L 160 95 L 128 64 L 128 0 L 192 0 Z" />
            </svg>
            <span className="text-sm font-normal tracking-tight text-white">arcane</span>
          </Link>
          <LiveDuelPill />
          <nav className="flex items-center gap-1">
            {NAV.map((item) => {
              const active = isActive(pathname, item.href);
              return (
                <Link
                  key={item.href}
                  href={item.href}
                  className={[
                    "rounded-md px-2.5 py-1 font-mono text-[11px] transition-colors",
                    active
                      ? "bg-primary/15 text-primary"
                      : "text-muted-foreground hover:bg-muted/50 hover:text-foreground",
                  ].join(" ")}
                >
                  {item.label}
                </Link>
              );
            })}
          </nav>
        </div>
        <div className="flex items-center gap-2">
          <ConnectWallet compact />
          <ModeToggle />
        </div>
      </div>
    </header>
  );
}
