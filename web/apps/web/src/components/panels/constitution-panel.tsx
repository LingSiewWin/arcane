"use client";

import { ShieldAlert, ShieldCheck } from "lucide-react";

import { Badge } from "@web/ui/components/badge";
import { Card } from "@web/ui/components/card";

import { shortHash } from "@/lib/format";
import type { RunStep } from "@/lib/run-types";

import { Mono, PanelTitle, TxLink } from "./primitives";

const RULE_DESCRIPTIONS: Record<string, string> = {
  MAX_LEVERAGE: "caps leverage per position",
  MAX_TRADE_SIZE: "caps notional size per trade",
  VENUE_BLACKLIST: "blocks disallowed venues",
};

export function ConstitutionPanel({
  spawn,
  revert,
}: {
  spawn: RunStep | undefined;
  revert: RunStep | undefined;
}) {
  const rules = spawn?.evidence.rule_kinds ?? [];
  const constitutionHash =
    spawn?.evidence.constitution_hash ?? spawn?.evidence.constitution_hash_onchain;
  const hashMatch =
    spawn?.evidence.constitution_hash_onchain !== undefined &&
    spawn?.evidence.constitution_hash_onchain === spawn?.evidence.constitution_hash_local;

  const ev = revert?.evidence;
  const violatedRule = ev?.expected_rule ?? "MAX_TRADE_SIZE";
  const attempted = ev?.oversize_usdc;
  const reason = ev?.revert_reason ?? "";
  // Pull the clean violation string out of the raw rpc error.
  const violationString = reason.includes("ConstitutionViolation")
    ? `ConstitutionViolation:${reason.split("ConstitutionViolation:")[1]?.split(/['"]/)[0] ?? violatedRule}`
    : `ConstitutionViolation:${violatedRule}`;
  const revertTx = revert?.tx_hash ?? ev?.tx_hash;

  return (
    <Card className="relative gap-0 overflow-hidden border-[--color-alarm]/40 bg-gradient-to-b from-[--color-alarm]/10 to-card p-6">
      <div
        aria-hidden="true"
        className="pointer-events-none absolute -right-16 -top-16 size-48 rounded-full bg-[--color-alarm]/10 blur-3xl"
      />

      <div className="flex items-center justify-between">
        <PanelTitle index="03" title="Constitution" subtitle="execution authority, bounded on-chain" />
        <Badge variant="outline" className="gap-1 border-[--color-alarm]/50 text-[--color-alarm]">
          <ShieldAlert className="size-3" /> reverted on-chain
        </Badge>
      </div>

      <div className="mt-5 grid gap-6 lg:grid-cols-[1fr_1.2fr]">
        {/* rule set */}
        <div className="flex flex-col gap-3">
          <span className="text-[10px] font-medium uppercase tracking-wider text-muted-foreground">
            Active rule set
          </span>
          <ul className="flex flex-col gap-2">
            {rules.map((r) => (
              <li
                key={r}
                className="flex items-center justify-between rounded-md border border-border/60 bg-card/50 px-3 py-2"
              >
                <div className="flex items-center gap-2">
                  <ShieldCheck className="size-3.5 text-primary" />
                  <span className="font-mono text-xs">{r}</span>
                </div>
                <span className="text-[10px] text-muted-foreground">{RULE_DESCRIPTIONS[r] ?? ""}</span>
              </li>
            ))}
          </ul>
          <div className="mt-1 flex items-center justify-between rounded-md bg-muted/40 px-3 py-2">
            <span className="text-[10px] uppercase tracking-wider text-muted-foreground">
              constitution hash
            </span>
            <div className="flex items-center gap-2">
              <Mono title={constitutionHash}>{shortHash(constitutionHash, 10, 6)}</Mono>
              {hashMatch ? (
                <Badge variant="outline" className="text-[9px] text-[--color-ok]">
                  on-chain = local
                </Badge>
              ) : null}
            </div>
          </div>
        </div>

        {/* the revert */}
        <div className="flex flex-col gap-3">
          <span className="text-[10px] font-medium uppercase tracking-wider text-muted-foreground">
            The agent tried to over-trade. The chain said no.
          </span>

          <div className="grid grid-cols-2 gap-3">
            <div className="rounded-md border border-border/60 bg-card/50 p-3">
              <span className="text-[10px] uppercase tracking-wider text-muted-foreground">
                attempted trade
              </span>
              <div className="mt-1 font-mono text-lg text-[--color-alarm]">
                {attempted !== undefined ? `${attempted} USDC` : "—"}
              </div>
              <span className="text-[10px] text-muted-foreground">
                {ev?.amount_units !== undefined ? `${ev.amount_units} base units` : ""}
              </span>
            </div>
            <div className="rounded-md border border-border/60 bg-card/50 p-3">
              <span className="text-[10px] uppercase tracking-wider text-muted-foreground">
                receipt status
              </span>
              <div className="mt-1 font-mono text-lg text-[--color-alarm]">
                {ev?.receipt_status ?? 0} · revert
              </div>
              <span className="text-[10px] text-muted-foreground">
                block #{ev?.block_number ?? "—"}
              </span>
            </div>
          </div>

          <div className="rounded-md border border-[--color-alarm]/40 bg-[--color-alarm]/5 p-3">
            <span className="text-[10px] uppercase tracking-wider text-muted-foreground">
              revert reason
            </span>
            <div className="mt-1 break-all font-mono text-sm text-[--color-alarm]">
              {violationString}
            </div>
          </div>

          <div className="flex items-center justify-between">
            <p className="max-w-sm text-xs leading-relaxed text-muted-foreground">
              The agent was{" "}
              <span className="text-foreground">mathematically prevented from over-trading</span>,
              on-chain — not by a backend check, by the validator hook.
            </p>
            <TxLink hash={revertTx} label="view revert tx" />
          </div>
        </div>
      </div>
    </Card>
  );
}
