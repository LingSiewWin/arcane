"use client";

/**
 * ArenaMemory — the headline differentiator made legible: this arena runs on a
 * genuinely 1-bit agent memory (RaBitQ), not FP32.
 *
 * The compression figures are DETERMINISTIC math from the embedding dim (they
 * mirror MemoryService.memory_stats exactly: ceil(d/8) bit code + 4 B l1 + 4 B
 * norm, vs d*4 for FP32) — not marketing numbers. The recall figure is a
 * measured result from `bench/measure_memory_efficiency.py` on the real MiniLM
 * trade-reasoning corpus, cited with provenance so it's verifiable, not a claim.
 */

import { Card } from "@web/ui/components/card";

import { PanelTitle } from "@/components/panels/primitives";

const EMBED_DIM = 384; // MiniLM all-MiniLM-L6-v2 — the arena's embedder.

function memoryStats(dim: number) {
  const codeBytes = Math.ceil(dim / 8); // packed sign bits
  const bytesPerVec = codeBytes + 4 + 4; // + l1 (f32) + norm (f32)
  const fp32 = dim * 4;
  return { bytesPerVec, fp32, compressionX: fp32 / bytesPerVec };
}

// Measured on 10k real MiniLM vectors vs brute-force FP32 ground truth.
// Reproduce: agents/.venv/bin/python -m bench.measure_memory_efficiency --n 10000
const MEASURED_RECALL_AT_10 = 77.4;

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

export function ArenaMemory() {
  const s = memoryStats(EMBED_DIM);
  return (
    <Card className="flex flex-col gap-3 p-4">
      <PanelTitle index="00" title="Memory efficiency" subtitle="1-bit RaBitQ agent memory" />
      <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
        <Stat
          label="compression"
          value={`${s.compressionX.toFixed(1)}×`}
          sub="vs FP32"
        />
        <Stat
          label="per vector"
          value={`${s.bytesPerVec} B`}
          sub={`FP32: ${s.fp32.toLocaleString()} B`}
        />
        <Stat
          label="recall@10"
          value={`${MEASURED_RECALL_AT_10}%`}
          sub="measured, real corpus"
        />
        <Stat label="embedder" value={`${EMBED_DIM}-d`} sub="MiniLM L6 v2" />
      </div>
      <p className="font-mono text-[10px] leading-relaxed text-muted-foreground/70">
        Agent reasoning is stored as 1-bit RaBitQ codes (sign bits + an L1 scalar) — no FP32
        vectors retained. {s.bytesPerVec} B/vector means an agent&apos;s whole memory is ~{s.compressionX.toFixed(0)}× cheaper to
        store, anchor on-chain, and search. Compression is deterministic from the {EMBED_DIM}-d
        embedder; recall is measured by <span className="text-foreground">bench/measure_memory_efficiency.py</span>.
      </p>
    </Card>
  );
}
