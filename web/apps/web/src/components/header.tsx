"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

import { ConnectWallet } from "./connect-wallet";
import { ModeToggle } from "./mode-toggle";

const NAV = [
  { href: "/arena", label: "arena" },
  { href: "/colosseum", label: "colosseum" },
  { href: "/console", label: "single run" },
] as const;

function isActive(pathname: string, href: string): boolean {
  if (href === "/") return pathname === "/";
  return pathname === href || pathname.startsWith(`${href}/`);
}

export default function Header() {
  const pathname = usePathname() ?? "/";
  return (
    <header className="border-b border-border/60 bg-card/30 backdrop-blur">
      <div className="mx-auto flex w-full max-w-7xl items-center justify-between gap-4 px-4 py-2 sm:px-6">
        <div className="flex items-center gap-5">
          <Link href="/" className="flex items-center gap-2">
            <span className="size-2 rounded-full bg-primary" />
            <span className="font-mono text-xs tracking-tight">agent-arena</span>
            <span className="hidden font-mono text-[10px] text-muted-foreground sm:inline">
              // arc living economy
            </span>
          </Link>
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
