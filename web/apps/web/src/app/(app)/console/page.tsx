"use client";

import { Skeleton } from "@web/ui/components/skeleton";

import { BondPanel } from "@/components/panels/bond-panel";
import { ConstitutionPanel } from "@/components/panels/constitution-panel";
import { EmptyState } from "@/components/panels/empty-state";
import { FlowTimeline } from "@/components/panels/flow-timeline";
import { HeaderPanel } from "@/components/panels/header-panel";
import { IdentityPanel } from "@/components/panels/identity-panel";
import { MemoryPanel } from "@/components/panels/memory-panel";
import { TxLedger } from "@/components/panels/tx-ledger";
import { useRun } from "@/lib/hooks";
import type { RunStep } from "@/lib/run-types";

function byName(steps: RunStep[], name: string): RunStep | undefined {
  return steps.find((s) => s.name === name);
}

export default function Home() {
  const run = useRun();

  return (
    <main className="mx-auto w-full max-w-6xl px-4 py-6 sm:px-6">
      <div className="flex flex-col gap-6">
        <HeaderPanel />

        {run.isPending ? (
          <div className="flex flex-col gap-6">
            <Skeleton className="h-28 w-full" />
            <Skeleton className="h-64 w-full" />
            <Skeleton className="h-48 w-full" />
          </div>
        ) : !run.data?.populated ? (
          <EmptyState message={run.data?.message} />
        ) : (
          (() => {
            const steps = run.data.steps;
            const spawn = byName(steps, "spawn_bob");
            const revert = byName(steps, "constitution_revert");
            const anchor = byName(steps, "anchor_pinned_root");
            const bond = byName(steps, "spawn_child_and_bond_resolve");
            return (
              <>
                <FlowTimeline steps={steps} />
                <ConstitutionPanel spawn={spawn} revert={revert} />
                <div className="grid gap-6 lg:grid-cols-2">
                  <IdentityPanel spawn={spawn} />
                  <BondPanel spawn={spawn} bond={bond} />
                </div>
                <MemoryPanel spawn={spawn} anchor={anchor} />
                <TxLedger steps={steps} />
              </>
            );
          })()
        )}

        <footer className="border-t border-border/60 pt-4 text-[10px] text-muted-foreground">
          Constrained Cognition · AgoraHack · reads scripts/demo_output.jsonl + live Arc testnet
          (chain 5042002) via the public RPC. Read-only. No keys, no private RPC.
        </footer>
      </div>
    </main>
  );
}
