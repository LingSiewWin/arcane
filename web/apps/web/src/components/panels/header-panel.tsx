"use client";

import { Badge } from "@web/ui/components/badge";

import { useBlockNumber } from "@/lib/hooks";
import { fmtNum } from "@/lib/format";

import { StatusDot } from "./primitives";

export function HeaderPanel() {
  const block = useBlockNumber();
  const connected = block.isSuccess;

  return (
    <header className="flex flex-col gap-3 border-b border-border/60 pb-5 sm:flex-row sm:items-start sm:justify-between">
      <div className="flex flex-col gap-1.5">
        <div className="flex items-center gap-2">
          <span className="font-mono text-[10px] uppercase tracking-[0.25em] text-primary/80">
            Constrained Cognition
          </span>
          <span className="font-mono text-[10px] text-muted-foreground">/ control room</span>
        </div>
        <h1 className="text-xl font-semibold tracking-tight sm:text-2xl">
          An autonomous trading agent whose every authority is{" "}
          <span className="text-primary">mathematically bounded</span>.
        </h1>
        <p className="max-w-2xl text-sm text-muted-foreground">
          Memory, identity, execution, spending and performance — all enforced on-chain on Arc.
          This dashboard reads the live run artifact and the chain directly. No mocks.
        </p>
      </div>

      <div className="flex shrink-0 flex-col items-start gap-2 sm:items-end">
        <div className="flex items-center gap-2 rounded-md border border-border/60 bg-card/60 px-3 py-2">
          <StatusDot tone={connected ? "ok" : block.isError ? "alarm" : "idle"} />
          <div className="flex flex-col leading-tight">
            <span className="text-xs font-medium">
              {connected ? "Connected to Arc" : block.isError ? "Arc RPC unreachable" : "Connecting…"}
            </span>
            <span className="font-mono text-[10px] text-muted-foreground">
              chain 5042002 · rpc.testnet.arc.network
            </span>
          </div>
        </div>
        <Badge variant="outline" className="font-mono text-[10px]">
          block #{block.data !== undefined ? fmtNum(block.data) : "—"}
        </Badge>
      </div>
    </header>
  );
}
