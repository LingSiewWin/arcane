"use client";

/**
 * ArenaMemory — the headline differentiator made LIVE: this arena runs on a
 * genuinely 1-bit agent memory (RaBitQ), and the panel proves it with the
 * agents' OWN on-chain data, not a static corpus.
 *
 * The compression figures are the MEASURED RaBitQ layout (56 B/vector vs 1536 B
 * FP32 → 27.4× on the 384-d MiniLM-L6-v2 embedder) — deterministic, not
 * marketing. What's live: each duelling agent's stored reasoning traces are
 * counted straight off `AgentReasoning` events, the byte figures are derived
 * from that real count, and the latest on-chain `MemoryAnchored` root is shown
 * as proof. Honest empty states when nothing is configured / live yet.
 */

import { ExternalLink } from "lucide-react";
import type { Address } from "viem";

import { Card } from "@web/ui/components/card";

import { PanelTitle } from "@/components/panels/primitives";
import { ARC_EXPLORER, addressUrl } from "@/lib/chain";
import {
  COLOSSEUM_CONFIGURED,
  MEMORY_ANCHOR,
  MEMORY_ANCHOR_CONFIGURED,
  MEMORY_BYTES_PER_VEC,
  MEMORY_COMPRESSION_X,
  MEMORY_EMBED_DIM,
  MEMORY_FP32_BYTES_PER_VEC,
  useActiveDuel,
  useAgentMemory,
} from "@/lib/colosseum";
import { shortHash } from "@/lib/format";

function fmtBytes(n: number): string {
  if (n <= 0) return "0 B";
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(2)} MB`;
}

function Stat({
  label,
  value,
  sub,
}: {
  label: string;
  value: string;
  sub?: string;
}) {
  return (
    <div className="flex flex-col gap-0.5">
      <span className="text-[10px] uppercase tracking-wider text-muted-foreground">{label}</span>
      <span className="font-mono text-lg tabular-nums text-[--color-ok]">{value}</span>
      {sub ? <span className="font-mono text-[10px] text-muted-foreground">{sub}</span> : null}
    </div>
  );
}

/** One agent's LIVE memory row: real trace count → compressed footprint → anchor proof. */
function AgentMemoryRow({ label, agent }: { label: string; agent: Address }) {
  const { data, isLoading } = useAgentMemory(agent);
  const entries = data?.entries ?? 0;

  return (
    <div className="flex flex-col gap-2 rounded-md border border-border/60 bg-muted/20 p-3">
      <div className="flex items-baseline justify-between gap-2">
        <span className="font-mono text-xs font-medium">{label}</span>
        <a
          href={addressUrl(agent)}
          target="_blank"
          rel="noreferrer"
          className="font-mono text-[10px] text-muted-foreground underline-offset-4 hover:text-foreground hover:underline"
        >
          {shortHash(agent)}
        </a>
      </div>

      {isLoading && !data ? (
        <span className="font-mono text-[10px] text-muted-foreground">reading on-chain…</span>
      ) : entries === 0 ? (
        <span className="font-mono text-[10px] text-muted-foreground">
          no reasoning stored yet
        </span>
      ) : (
        <>
          <div className="grid grid-cols-3 gap-3">
            <Stat label="traces stored" value={entries.toLocaleString()} sub="on-chain reasoning" />
            <Stat
              label="memory (1-bit)"
              value={fmtBytes(data?.bytes ?? 0)}
              sub={`FP32: ${fmtBytes(data?.fp32 ?? 0)}`}
            />
            <Stat
              label="saved"
              value={`${MEMORY_COMPRESSION_X.toFixed(1)}×`}
              sub={`${fmtBytes((data?.fp32 ?? 0) - (data?.bytes ?? 0))} less`}
            />
          </div>
          <div className="font-mono text-[10px]">
            {data?.anchoredRoot ? (
              <a
                href={
                  MEMORY_ANCHOR_CONFIGURED
                    ? addressUrl(MEMORY_ANCHOR)
                    : ARC_EXPLORER
                }
                target="_blank"
                rel="noreferrer"
                className="inline-flex items-center gap-1 text-[--color-ok] underline-offset-4 hover:underline"
                title={data.anchoredRoot}
              >
                anchored ✓ {shortHash(data.anchoredRoot)}
                <ExternalLink className="size-3 opacity-60" />
              </a>
            ) : (
              <span className="text-muted-foreground">not yet anchored</span>
            )}
          </div>
        </>
      )}
    </div>
  );
}

export function ArenaMemory() {
  const { data: duel } = useActiveDuel();

  // Headline math is deterministic from the embedder; show it always.
  const headline = (
    <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
      <Stat label="compression" value={`${MEMORY_COMPRESSION_X.toFixed(1)}×`} sub="vs FP32" />
      <Stat
        label="per vector"
        value={`${MEMORY_BYTES_PER_VEC} B`}
        sub={`FP32: ${MEMORY_FP32_BYTES_PER_VEC.toLocaleString()} B`}
      />
      <Stat label="codec" value="1-bit" sub="RaBitQ sign codes" />
      <Stat label="embedder" value={`${MEMORY_EMBED_DIM}-d`} sub="MiniLM L6 v2" />
    </div>
  );

  let body: React.ReactNode;
  if (!COLOSSEUM_CONFIGURED) {
    body = (
      <p className="font-mono text-[10px] leading-relaxed text-muted-foreground/70">
        Colosseum not configured — set <span className="text-foreground">NEXT_PUBLIC_COLOSSEUM</span>{" "}
        to read live per-agent memory. The compression layout above is deterministic from the{" "}
        {MEMORY_EMBED_DIM}-d embedder.
      </p>
    );
  } else if (!duel || (!duel.agentA && !duel.agentB)) {
    body = (
      <p className="font-mono text-[10px] leading-relaxed text-muted-foreground/70">
        No live agents yet — start a duel and each agent&apos;s real reasoning traces will be
        counted and compressed here.
      </p>
    );
  } else {
    body = (
      <div className="grid gap-3 sm:grid-cols-2">
        {duel.agentA ? <AgentMemoryRow label="Agent A" agent={duel.agentA} /> : null}
        {duel.agentB ? <AgentMemoryRow label="Agent B" agent={duel.agentB} /> : null}
      </div>
    );
  }

  return (
    <section className="flex flex-col gap-3">
      <PanelTitle index="00" title="Memory efficiency" subtitle="1-bit RaBitQ agent memory · live" />
      <Card className="flex flex-col gap-3 p-4">
        {headline}
        {body}
        <p className="font-mono text-[10px] leading-relaxed text-muted-foreground/70">
          This is the compression of each agent&apos;s REAL reasoning traces (counted live from
          on-chain <span className="text-foreground">AgentReasoning</span> events), not a static
          corpus. Every trace is stored as a {MEMORY_BYTES_PER_VEC} B 1-bit RaBitQ code (sign bits +
          an L1 scalar) instead of a {MEMORY_FP32_BYTES_PER_VEC.toLocaleString()} B FP32 vector — ~
          {MEMORY_COMPRESSION_X.toFixed(0)}× cheaper to store, anchor on-chain, and search. The
          anchored root is the on-chain proof of that compressed memory.
        </p>
      </Card>
    </section>
  );
}
