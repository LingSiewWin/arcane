"use client";

import { ExternalLink } from "lucide-react";
import type { ReactNode } from "react";

import { txUrl } from "@/lib/chain";
import { shortHash } from "@/lib/format";

/** Monospace external link to an Arc tx, truncated. */
export function TxLink({
  hash,
  label,
  className = "",
}: {
  hash: string | undefined;
  label?: string;
  className?: string;
}) {
  if (!hash) return <span className="text-muted-foreground">—</span>;
  return (
    <a
      href={txUrl(hash)}
      target="_blank"
      rel="noreferrer"
      className={`group inline-flex items-center gap-1 font-mono text-xs text-primary/90 underline-offset-4 transition-colors hover:text-primary hover:underline ${className}`}
    >
      <span>{label ?? shortHash(hash)}</span>
      <ExternalLink className="size-3 opacity-50 transition-opacity group-hover:opacity-100" />
    </a>
  );
}

/** A bare monospace value, optionally a full address/hash. */
export function Mono({
  children,
  className = "",
  title,
}: {
  children: ReactNode;
  className?: string;
  title?: string;
}) {
  return (
    <span title={title} className={`font-mono text-xs ${className}`}>
      {children}
    </span>
  );
}

/** Label + value stat row. */
export function Stat({
  label,
  children,
  hint,
}: {
  label: string;
  children: ReactNode;
  hint?: string;
}) {
  return (
    <div className="flex flex-col gap-0.5">
      <span className="text-[10px] font-medium uppercase tracking-wider text-muted-foreground">
        {label}
      </span>
      <span className="text-sm tabular-nums">{children}</span>
      {hint ? <span className="text-[10px] text-muted-foreground">{hint}</span> : null}
    </div>
  );
}

/** Pulsing status dot. Conveys state to assistive tech via role/label, not color alone. */
export function StatusDot({ tone, label }: { tone: "ok" | "alarm" | "idle"; label?: string }) {
  const color =
    tone === "ok" ? "bg-[--color-ok]" : tone === "alarm" ? "bg-[--color-alarm]" : "bg-muted-foreground";
  const stateLabel =
    label ?? (tone === "ok" ? "live" : tone === "alarm" ? "error" : "connecting");
  return (
    <span role="status" aria-label={stateLabel} className="relative flex size-2">
      {tone === "ok" ? (
        <span
          aria-hidden="true"
          className={`absolute inline-flex size-full animate-ping rounded-full ${color} opacity-60`}
        />
      ) : null}
      <span aria-hidden="true" className={`relative inline-flex size-2 rounded-full ${color}`} />
    </span>
  );
}

/** Section header used inside panels. */
export function PanelTitle({
  index,
  title,
  subtitle,
}: {
  index?: string;
  title: string;
  subtitle?: string;
}) {
  return (
    <div className="flex items-baseline gap-2">
      {index ? (
        <span className="font-mono text-[10px] text-muted-foreground">{index}</span>
      ) : null}
      <h2 className="text-sm font-semibold tracking-tight">{title}</h2>
      {subtitle ? <span className="text-xs text-muted-foreground">{subtitle}</span> : null}
    </div>
  );
}
