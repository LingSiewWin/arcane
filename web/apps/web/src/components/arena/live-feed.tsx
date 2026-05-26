"use client";

import { AnimatePresence, motion } from "framer-motion";
import { Activity, BadgeCheck, Coins, FileText, ShieldAlert, ShieldX } from "lucide-react";
import Link from "next/link";
import type { ComponentType } from "react";
import { useEffect, useState } from "react";

import { Card } from "@web/ui/components/card";

import { REGISTRY_CONFIGURED, useLiveFeed, type FeedEvent } from "@/lib/arena";
import { txUrl } from "@/lib/chain";
import { ACTION_KINDS, type ActionKind } from "@/lib/constants";
import { relativeTime, shortHash } from "@/lib/format";

import { PanelTitle, StatusDot, TxLink } from "@/components/panels/primitives";

import { ArenaEmpty } from "./arena-empty";

const ICONS: Record<ActionKind, ComponentType<{ className?: string }>> = {
  0: FileText,
  1: Coins,
  2: ShieldAlert,
  3: ShieldX,
  4: BadgeCheck,
};

const TONE_CLASS: Record<"ok" | "signal" | "alarm", string> = {
  ok: "bg-[--color-ok]/15 text-[--color-ok]",
  signal: "bg-primary/15 text-primary",
  alarm: "bg-[--color-alarm]/15 text-[--color-alarm]",
};

/** Tick once a second so relative timestamps stay fresh without re-fetching. */
function useNow(intervalMs = 1_000): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), intervalMs);
    return () => clearInterval(id);
  }, [intervalMs]);
  return now;
}

/** A trace field that, when present, is a deterministic function of on-chain
 *  state — the Substance Bar: every visible value resolves to the event. */
function TraceChip({ label, value }: { label: string; value: string }) {
  return (
    <span className="inline-flex items-center gap-1 rounded border border-border/60 px-1.5 py-0.5 font-mono text-[9px] uppercase tracking-wide text-muted-foreground">
      <span className="text-muted-foreground/60">{label}</span>
      <span className="text-foreground">{value}</span>
    </span>
  );
}

function FeedRow({ event, now }: { event: FeedEvent; now: number }) {
  const meta = ACTION_KINDS[event.kind];
  const Icon = ICONS[event.kind] ?? Activity;
  const [open, setOpen] = useState(false);
  const advice = event.trace.advice;
  const bps = event.trace.resolveBps;
  const hasTrace = advice !== undefined || bps !== undefined;

  return (
    <motion.li
      layout
      initial={{ opacity: 0, y: -8, scale: 0.99 }}
      animate={{ opacity: 1, y: 0, scale: 1 }}
      exit={{ opacity: 0 }}
      transition={{ duration: 0.3, ease: [0.16, 1, 0.3, 1] }}
      className="flex flex-col rounded-md border border-border/50 bg-card/40"
    >
      <div className="flex items-center gap-3 px-3 py-2">
        <span
          className={`flex size-7 shrink-0 items-center justify-center rounded-md ${TONE_CLASS[meta.tone]}`}
        >
          <Icon className="size-3.5" />
        </span>
        <button
          type="button"
          onClick={() => hasTrace && setOpen((v) => !v)}
          aria-expanded={hasTrace ? open : undefined}
          className={`flex min-w-0 flex-1 flex-col items-start text-left leading-tight ${
            hasTrace ? "cursor-pointer" : "cursor-default"
          }`}
        >
          <span className="truncate text-xs">
            <span className="font-mono text-primary/90">Agent #{event.agentId}</span>{" "}
            <span className="text-foreground">{meta.label}</span>
            {advice ? (
              <span className="ml-1.5 font-mono text-[10px] text-[--color-signal]">
                {advice.symbol} · {advice.stance}
              </span>
            ) : null}
            {bps !== undefined ? (
              <span
                className={`ml-1.5 font-mono text-[10px] ${
                  bps >= 0 ? "text-[--color-ok]" : "text-[--color-alarm]"
                }`}
              >
                {bps >= 0 ? "+" : ""}
                {bps} bps
              </span>
            ) : null}
          </span>
          <span className="font-mono text-[10px] text-muted-foreground">
            {relativeTime(event.timestamp, now)} · {shortHash(event.txHash)}
            {hasTrace ? <span className="ml-1 text-muted-foreground/60">· {open ? "hide" : "trace"}</span> : null}
          </span>
        </button>
        <TxLink hash={event.txHash} label="tx" />
      </div>

      {open && advice ? (
        <div className="flex flex-col gap-2 border-t border-border/40 px-3 py-2.5">
          <p className="text-xs leading-relaxed text-foreground/90">{advice.reasoning}</p>
          <div className="flex flex-wrap items-center gap-1.5">
            <TraceChip label="asset" value={advice.symbol || "—"} />
            <TraceChip label="stance" value={advice.stance} />
            <TraceChip label="advice" value={shortHash(advice.adviceHash)} />
          </div>
          <p className="font-mono text-[9px] text-muted-foreground/70">
            mandate →{" "}
            <Link href={`/arena/${event.agentId}`} className="text-primary/80 hover:underline">
              agent #{event.agentId} constitution
            </Link>{" "}
            · settled on-chain · decoded from AgentAction payload
          </p>
        </div>
      ) : null}
    </motion.li>
  );
}

export function LiveFeed() {
  const events = useLiveFeed();
  const now = useNow();

  // Polite, non-spammy announcement: one summary line for assistive tech that
  // updates only when the newest event changes, rather than reading every row.
  const latest = events[0];
  const announcement = latest
    ? `Live activity: ${events.length} actions. Latest — agent ${latest.agentId} ${ACTION_KINDS[latest.kind].label}.`
    : "";

  return (
    <section className="flex h-full flex-col gap-3">
      <div className="flex items-center justify-between">
        <PanelTitle index="02" title="Live activity" subtitle="AgentAction stream" />
        <span className="inline-flex items-center gap-1.5">
          <StatusDot tone={REGISTRY_CONFIGURED ? "ok" : "idle"} label={REGISTRY_CONFIGURED ? "watching" : "idle"} />
          <span className="font-mono text-[10px] text-muted-foreground">
            {REGISTRY_CONFIGURED ? "polling 4s" : "idle"}
          </span>
        </span>
      </div>
      <Card className="flex flex-1 flex-col gap-3 p-4">
      <p aria-live="polite" aria-atomic="true" className="sr-only">
        {announcement}
      </p>

      {!REGISTRY_CONFIGURED ? (
        <ArenaEmpty title="Feed idle">
          Set <span className="font-mono">NEXT_PUBLIC_AGENT_REGISTRY</span> to stream live
          AgentAction events.
        </ArenaEmpty>
      ) : events.length === 0 ? (
        <div className="flex flex-1 flex-col items-center justify-center gap-2 py-10 text-center">
          <Activity className="size-6 text-muted-foreground" />
          <p className="max-w-xs text-xs text-muted-foreground">
            Watching the registry — no actions yet. New advice, queries, reverts and bond
            slashes/releases will stream in here.
          </p>
        </div>
      ) : (
        <ul
          aria-label="Live AgentAction feed"
          className="flex min-h-[24rem] flex-1 flex-col gap-2 overflow-y-auto pr-1"
        >
          <AnimatePresence initial={false}>
            {events.slice(0, 6).map((e) => (
              <FeedRow key={e.id} event={e} now={now} />
            ))}
          </AnimatePresence>
        </ul>
      )}
      </Card>
    </section>
  );
}
