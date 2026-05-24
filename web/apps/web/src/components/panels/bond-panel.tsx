"use client";

import { Activity } from "lucide-react";
import type { Address } from "viem";

import { Badge } from "@web/ui/components/badge";
import { Card } from "@web/ui/components/card";
import { Skeleton } from "@web/ui/components/skeleton";

import { fmtBond6, fmtTime, fmtUsd } from "@/lib/format";
import { useBondBalance, usePythSolUsd } from "@/lib/hooks";
import type { RunStep } from "@/lib/run-types";

import { PanelTitle, Stat, StatusDot, TxLink } from "./primitives";

export function BondPanel({ spawn, bond }: { spawn: RunStep | undefined; bond: RunStep | undefined }) {
  const pyth = usePythSolUsd();

  const vault = spawn?.evidence.addresses?.BondVault?.address as Address | undefined;
  const agent = (bond?.evidence.child_eoa ?? spawn?.evidence.eoa) as Address | undefined;
  const bal = useBondBalance(vault, agent);

  const advice = bond?.evidence.record_advice;
  const p0 = advice?.hermes_p0_float;
  const resolved = bond?.evidence.bond_resolved;
  const resolveErr = bond?.evidence.error;
  const recordTx = advice?.record_advice_tx;
  const postTx = bond?.evidence.bond_post?.post_tx;

  return (
    <Card className="gap-0 p-5">
      <PanelTitle index="05" title="Bond & oracle" subtitle="spending authority, oracle-priced" />

      <div className="mt-4 grid gap-5 md:grid-cols-3">
        {/* live pyth */}
        <div className="flex flex-col gap-2 rounded-md border border-border/60 bg-card/40 p-3">
          <div className="flex items-center justify-between">
            <span className="text-[10px] font-medium uppercase tracking-wider text-muted-foreground">
              SOL / USD · Pyth live
            </span>
            <StatusDot tone={pyth.isSuccess ? "ok" : pyth.isError ? "alarm" : "idle"} />
          </div>
          {pyth.isPending ? (
            <Skeleton className="h-9 w-32" />
          ) : pyth.isError ? (
            <div className="text-sm text-muted-foreground">price unavailable</div>
          ) : (
            <div className="font-mono text-3xl tabular-nums text-primary">
              {fmtUsd(pyth.data?.value)}
            </div>
          )}
          <div className="flex items-center gap-1 text-[10px] text-muted-foreground">
            <Activity className="size-3" />
            {pyth.isSuccess
              ? `pub ${fmtTime(pyth.data?.publishTime)} · refresh 10s`
              : "0x2880…7B43 getPriceUnsafe"}
          </div>
        </div>

        {/* recorded entry */}
        <div className="flex flex-col gap-3">
          <Stat label="recorded entry p0" hint="Hermes price pinned at advice time">
            <span className="font-mono">{p0 !== undefined ? fmtUsd(p0) : "—"}</span>
          </Stat>
          <Stat label="agent bond (chain)">
            {bal.isPending ? (
              <Skeleton className="h-4 w-16" />
            ) : bal.isError ? (
              <span className="text-muted-foreground">unavailable</span>
            ) : (
              <span className="font-mono">{fmtBond6(bal.data)} USDC</span>
            )}
          </Stat>
          <Stat label="child budget">
            <span className="font-mono">
              {bond?.evidence.child_budget_usdc !== undefined
                ? `${bond.evidence.child_budget_usdc} USDC`
                : "—"}
            </span>
          </Stat>
        </div>

        {/* resolve outcome */}
        <div className="flex flex-col gap-3 rounded-md border border-border/60 bg-card/40 p-3">
          <span className="text-[10px] font-medium uppercase tracking-wider text-muted-foreground">
            Resolve outcome
          </span>
          {resolved === true ? (
            <Badge variant="outline" className="w-fit text-[--color-ok]">
              released
            </Badge>
          ) : resolved === false ? (
            <Badge variant="outline" className="w-fit border-[--color-alarm]/50 text-[--color-alarm]">
              not resolved
            </Badge>
          ) : (
            <Badge variant="outline" className="w-fit">
              pending
            </Badge>
          )}
          {resolveErr ? (
            <p className="break-words font-mono text-[10px] leading-relaxed text-muted-foreground">
              {resolveErr}
            </p>
          ) : null}
          <div className="mt-auto flex flex-col gap-1 border-t border-border/50 pt-2">
            <div className="flex items-center justify-between">
              <span className="text-[10px] text-muted-foreground">bond post</span>
              <TxLink hash={postTx} />
            </div>
            <div className="flex items-center justify-between">
              <span className="text-[10px] text-muted-foreground">record advice</span>
              <TxLink hash={recordTx} />
            </div>
          </div>
        </div>
      </div>
      <p className="mt-3 text-xs text-muted-foreground">
        The bond is priced by a <span className="text-foreground">real oracle</span> — the same Pyth
        feed streaming above sets the entry price the agent is held to.
      </p>
    </Card>
  );
}
