"use client";

/**
 * ArenaMemory — slim footer strip (demoted from the former hero block). Stays
 * truthful and LIVE: it still reads each duelling agent's stored reasoning
 * traces straight off `AgentReasoning` events (via useAgentMemory) and surfaces
 * the latest on-chain `MemoryAnchored` root as proof. The compression figure is
 * the MEASURED RaBitQ layout (56 B/vector vs 1536 B FP32 → 27.4× on the 384-d
 * MiniLM-L6-v2 embedder) — deterministic, not marketing. One line, no hero.
 */

import { ExternalLink } from "lucide-react";
import type { Address, Hex } from "viem";

import { PanelTitle } from "@/components/panels/primitives";
import { ARC_EXPLORER, addressUrl } from "@/lib/chain";
import {
  COLOSSEUM_CONFIGURED,
  MEMORY_ANCHOR,
  MEMORY_ANCHOR_CONFIGURED,
  MEMORY_COMPRESSION_X,
  useActiveDuel,
  useAgentMemory,
} from "@/lib/colosseum";
import { shortHash } from "@/lib/format";

/** Pick the freshest live anchored root across the duel's agents, if any. */
function useLatestAnchor(
  agentA: Address | undefined,
  agentB: Address | undefined,
): { root: Hex | null; traces: number } {
  const a = useAgentMemory(agentA);
  const b = useAgentMemory(agentB);
  const traces = (a.data?.entries ?? 0) + (b.data?.entries ?? 0);
  // Prefer A's anchor, fall back to B's — both are read from MemoryAnchored.
  const root = a.data?.anchoredRoot ?? b.data?.anchoredRoot ?? null;
  return { root, traces };
}

export function ArenaMemory() {
  const { data: duel } = useActiveDuel();
  const { root, traces } = useLatestAnchor(duel?.agentA, duel?.agentB);

  const compression = `${MEMORY_COMPRESSION_X.toFixed(0)}× vs FP32`;

  return (
    <section className="flex flex-col gap-2">
      <PanelTitle index="·" title="Memory" subtitle="1-bit RaBitQ · on-chain" />
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 rounded-md border border-border/60 bg-muted/20 px-3 py-2 font-mono text-[10px] text-muted-foreground">
        <span>
          1-bit RaBitQ memory · <span className="text-[--color-ok]">{compression}</span>
        </span>
        {COLOSSEUM_CONFIGURED && traces > 0 ? (
          <span className="text-foreground/70">{traces.toLocaleString()} live traces</span>
        ) : null}
        {root ? (
          <a
            href={MEMORY_ANCHOR_CONFIGURED ? addressUrl(MEMORY_ANCHOR) : ARC_EXPLORER}
            target="_blank"
            rel="noreferrer"
            className="inline-flex items-center gap-1 text-[--color-ok] underline-offset-4 hover:underline"
            title={root}
          >
            anchored ✓ {shortHash(root)}
            <ExternalLink className="size-3 opacity-60" />
          </a>
        ) : (
          <span>not yet anchored</span>
        )}
      </div>
    </section>
  );
}
