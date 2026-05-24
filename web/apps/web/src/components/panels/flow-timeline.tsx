"use client";

import { Check, ShieldAlert, X } from "lucide-react";

import { Card } from "@web/ui/components/card";

import type { RunStep } from "@/lib/run-types";

import { PanelTitle, TxLink } from "./primitives";

const STEP_LABELS: Record<string, string> = {
  spawn_bob: "spawn",
  query_alice: "query",
  select_violating_trace: "select",
  constitution_revert: "constitution revert",
  anchor_pinned_root: "anchor",
  spawn_child_and_bond_resolve: "bond resolve",
};

function stepTx(step: RunStep): string | undefined {
  return step.tx_hash ?? step.evidence.tx_hash ?? step.evidence.explorer_url?.split("/tx/")[1];
}

export function FlowTimeline({ steps }: { steps: RunStep[] }) {
  return (
    <Card className="gap-0 p-5">
      <PanelTitle index="02" title="Agent flow" subtitle="six on-chain steps, one live run" />
      <div className="mt-5 grid grid-cols-2 gap-3 md:grid-cols-3 lg:grid-cols-6">
        {steps.map((step, i) => {
          const isRevert = step.name === "constitution_revert";
          const tone = isRevert ? "revert" : step.ok ? "ok" : "fail";
          const tx = stepTx(step);
          return (
            <div key={step.step} className="relative flex flex-col gap-2">
              <div
                className={[
                  "flex items-center gap-2 rounded-md border px-2.5 py-2",
                  tone === "revert"
                    ? "border-[--color-alarm]/50 bg-[--color-alarm]/10"
                    : "border-border/60 bg-card/40",
                ].join(" ")}
              >
                <span
                  className={[
                    "flex size-5 shrink-0 items-center justify-center rounded-full text-[10px]",
                    tone === "revert"
                      ? "bg-[--color-alarm]/20 text-[--color-alarm]"
                      : tone === "ok"
                        ? "bg-[--color-ok]/15 text-[--color-ok]"
                        : "bg-destructive/20 text-destructive",
                  ].join(" ")}
                >
                  {tone === "revert" ? (
                    <ShieldAlert className="size-3" />
                  ) : tone === "ok" ? (
                    <Check className="size-3" />
                  ) : (
                    <X className="size-3" />
                  )}
                </span>
                <div className="flex min-w-0 flex-col leading-tight">
                  <span className="font-mono text-[10px] text-muted-foreground">step {step.step}</span>
                  <span className="truncate text-xs font-medium capitalize">
                    {STEP_LABELS[step.name] ?? step.name}
                  </span>
                </div>
              </div>
              <div className="px-0.5">
                {tx ? (
                  <TxLink hash={tx} />
                ) : (
                  <span className="font-mono text-[10px] text-muted-foreground">off-chain</span>
                )}
              </div>
              {i < steps.length - 1 ? (
                <span
                  aria-hidden="true"
                  className="pointer-events-none absolute right-[-10px] top-4 hidden text-muted-foreground/40 lg:block"
                >
                  →
                </span>
              ) : null}
            </div>
          );
        })}
      </div>
    </Card>
  );
}
