"use client";

import type { Address } from "viem";

import { Badge } from "@web/ui/components/badge";
import { Card } from "@web/ui/components/card";
import { Skeleton } from "@web/ui/components/skeleton";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@web/ui/components/table";

import { fmtNum, fmtTime, shortHash } from "@/lib/format";
import { useMemoryAnchor } from "@/lib/hooks";
import type { RunStep } from "@/lib/run-types";

import { Mono, PanelTitle, Stat, TxLink } from "./primitives";

/** Headline numbers from bench/RESULTS.md (published, embeddable). */
const BENCH = [
  { method: "FP32 Flat", bytes: "1536", size: "73.24", recall: "100.0%", cost: "$0.0329", winner: false },
  { method: "TurboQuant b=4", bytes: "192", size: "9.16", recall: "90.25%", cost: "$0.0041", winner: false },
  { method: "RaBitQ-1bit", bytes: "50", size: "2.38", recall: "65.7%*", cost: "$0.0011", winner: true },
];

export function MemoryPanel({ spawn, anchor }: { spawn: RunStep | undefined; anchor: RunStep | undefined }) {
  const anchorAddr = spawn?.evidence.addresses?.MemoryAnchor?.address as Address | undefined;
  const identityId = spawn?.evidence.addresses?.identity_id ?? anchor?.evidence.identity_id;
  const read = useMemoryAnchor(anchorAddr, identityId);

  const pinnedRoot = anchor?.evidence.root ?? anchor?.evidence.pinned_root_after;
  const anchorTx = anchor?.tx_hash ?? anchor?.evidence.tx_hash;
  const evicted = anchor?.evidence.evicted;
  const entriesAfter = anchor?.evidence.entries_after;

  return (
    <Card className="gap-0 p-5">
      <PanelTitle index="04" title="Memory" subtitle="bounded, compressed, Merkle-pinned" />

      <div className="mt-4 grid gap-5 lg:grid-cols-[1.3fr_1fr]">
        {/* benchmark table */}
        <div className="flex flex-col gap-2">
          <span className="text-[10px] font-medium uppercase tracking-wider text-muted-foreground">
            Vector store benchmark · 50k embeddings, 384-d
          </span>
          <div className="overflow-hidden rounded-md border border-border/60">
            <Table>
              <TableHeader>
                <TableRow className="border-border/60 hover:bg-transparent">
                  <TableHead className="h-8 text-[10px] uppercase">Method</TableHead>
                  <TableHead className="h-8 text-right text-[10px] uppercase">B/vec</TableHead>
                  <TableHead className="h-8 text-right text-[10px] uppercase">Index MB</TableHead>
                  <TableHead className="h-8 text-right text-[10px] uppercase">Recall@10</TableHead>
                  <TableHead className="h-8 text-right text-[10px] uppercase">$/M/mo</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {BENCH.map((r) => (
                  <TableRow
                    key={r.method}
                    className={r.winner ? "border-border/60 bg-primary/5" : "border-border/60"}
                  >
                    <TableCell className="py-2 text-xs font-medium">
                      <span className="flex items-center gap-1.5">
                        {r.method}
                        {r.winner ? (
                          <Badge variant="outline" className="text-[9px] text-primary">
                            30× cheaper
                          </Badge>
                        ) : null}
                      </span>
                    </TableCell>
                    <TableCell className="py-2 text-right font-mono text-xs tabular-nums">{r.bytes}</TableCell>
                    <TableCell className="py-2 text-right font-mono text-xs tabular-nums">{r.size}</TableCell>
                    <TableCell className="py-2 text-right font-mono text-xs tabular-nums">{r.recall}</TableCell>
                    <TableCell className="py-2 text-right font-mono text-xs tabular-nums">{r.cost}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
          <span className="text-[10px] text-muted-foreground">
            RaBitQ: 50 B/vec, 30× compression vs FP32. *raw 1-bit recall; rerank lifts to ~95%.
            Source: <span className="font-mono">bench/RESULTS.md</span>.
          </span>
        </div>

        {/* anchor + live read */}
        <div className="flex flex-col gap-3 rounded-md border border-border/60 bg-card/40 p-3">
          <span className="text-[10px] font-medium uppercase tracking-wider text-muted-foreground">
            MemoryAnchor · live read
          </span>
          <div className="rounded-md bg-muted/40 px-3 py-2">
            <span className="text-[10px] uppercase tracking-wider text-muted-foreground">
              pinned Merkle root
            </span>
            <div className="mt-0.5 break-all font-mono text-xs text-primary/90">
              {pinnedRoot ? shortHash(pinnedRoot, 14, 8) : "—"}
            </div>
          </div>

          <div className="grid grid-cols-2 gap-3">
            <Stat label="identity">{identityId !== undefined ? `#${identityId}` : "—"}</Stat>
            <Stat label="history len (chain)">
              {read.isPending ? (
                <Skeleton className="h-4 w-10" />
              ) : read.isError ? (
                <span className="text-muted-foreground">unavailable</span>
              ) : (
                fmtNum(read.data?.historyLength)
              )}
            </Stat>
            <Stat label="entries kept">{entriesAfter !== undefined ? fmtNum(entriesAfter) : "—"}</Stat>
            <Stat label="evicted">{evicted !== undefined ? fmtNum(evicted) : "—"}</Stat>
          </div>

          <div className="flex flex-col gap-1 border-t border-border/50 pt-2">
            <span className="text-[10px] uppercase tracking-wider text-muted-foreground">
              anchorAt[0] owner
            </span>
            {read.isPending ? (
              <Skeleton className="h-4 w-40" />
            ) : read.isError ? (
              <span className="text-xs text-muted-foreground">unavailable from public RPC</span>
            ) : (
              <Mono title={read.data?.ownerAtAnchor}>{shortHash(read.data?.ownerAtAnchor, 10, 6)}</Mono>
            )}
            <span className="text-[10px] text-muted-foreground">
              anchored {read.data ? fmtTime(read.data.timestamp) : "—"}
            </span>
          </div>

          <div className="flex items-center justify-between border-t border-border/50 pt-2">
            <span className="text-[10px] text-muted-foreground">anchor tx</span>
            <TxLink hash={anchorTx} />
          </div>
        </div>
      </div>
    </Card>
  );
}
