"use client";

import { Card } from "@web/ui/components/card";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@web/ui/components/table";

import type { RunStep } from "@/lib/run-types";

import { PanelTitle, TxLink } from "./primitives";

interface LedgerRow {
  label: string;
  hash: string;
  status: "ok" | "revert";
  step: number;
}

/** Collect every real tx hash referenced anywhere in the run. */
function collectTxs(steps: RunStep[]): LedgerRow[] {
  const rows: LedgerRow[] = [];
  const seen = new Set<string>();
  const push = (label: string, hash: string | undefined, step: number, status: "ok" | "revert") => {
    if (!hash || seen.has(hash)) return;
    seen.add(hash);
    rows.push({ label, hash, step, status });
  };

  for (const s of steps) {
    const ev = s.evidence;
    // contract deploys (step 1)
    if (ev.addresses) {
      const a = ev.addresses;
      push("deploy ConstitutionRegistry", a.ConstitutionRegistry?.tx_hash, s.step, "ok");
      push("deploy ConstitutionHook", a.ConstitutionHook?.tx_hash, s.step, "ok");
      push("deploy ConstitutionValidator", a.ConstitutionValidator?.tx_hash, s.step, "ok");
      push("deploy IdentityRegistry", a.IdentityRegistry?.tx_hash, s.step, "ok");
      push("mint identity", a.IdentityRegistry?.mint_tx, s.step, "ok");
      push("deploy MemoryAnchor", a.MemoryAnchor?.tx_hash, s.step, "ok");
      push("deploy BondVault", a.BondVault?.tx_hash, s.step, "ok");
      push("deploy PerformanceOracle", a.PerformanceOracle?.tx_hash, s.step, "ok");
      push("set oracle", a.PerformanceOracle?.set_oracle_tx, s.step, "ok");
      push("deploy GmxV2PerpAdapter", a.GmxV2PerpAdapter?.tx_hash, s.step, "ok");
    }
    if (ev.hook_install_tx) push("install constitution hook", ev.hook_install_tx, s.step, "ok");
    // primary step tx
    if (s.tx_hash || ev.tx_hash) {
      const isRevert = s.name === "constitution_revert" || ev.receipt_status === 0;
      push(s.name.replace(/_/g, " "), s.tx_hash ?? ev.tx_hash, s.step, isRevert ? "revert" : "ok");
    }
    // bond resolve sub-txs
    if (ev.bond_post) {
      push("bond approve", ev.bond_post.approve_tx, s.step, "ok");
      push("bond post", ev.bond_post.post_tx, s.step, "ok");
    }
    if (ev.fund_oracle_tx) push("fund oracle", ev.fund_oracle_tx, s.step, "ok");
    if (ev.record_advice?.record_advice_tx)
      push("record advice", ev.record_advice.record_advice_tx, s.step, "ok");
  }
  return rows;
}

export function TxLedger({ steps }: { steps: RunStep[] }) {
  const rows = collectTxs(steps);

  return (
    <Card className="gap-0 p-5">
      <PanelTitle index="07" title="Transaction ledger" subtitle={`${rows.length} on-chain txs`} />
      <div className="mt-4 overflow-hidden rounded-md border border-border/60">
        <Table>
          <TableHeader>
            <TableRow className="border-border/60 hover:bg-transparent">
              <TableHead className="h-8 w-12 text-[10px] uppercase">Step</TableHead>
              <TableHead className="h-8 text-[10px] uppercase">Operation</TableHead>
              <TableHead className="h-8 text-[10px] uppercase">Status</TableHead>
              <TableHead className="h-8 text-right text-[10px] uppercase">Tx</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {rows.map((r) => (
              <TableRow key={r.hash} className="border-border/50">
                <TableCell className="py-1.5 font-mono text-xs text-muted-foreground">{r.step}</TableCell>
                <TableCell className="py-1.5 text-xs capitalize">{r.label}</TableCell>
                <TableCell className="py-1.5">
                  <span
                    className={[
                      "inline-flex items-center gap-1 font-mono text-[10px]",
                      r.status === "revert" ? "text-[--color-alarm]" : "text-[--color-ok]",
                    ].join(" ")}
                  >
                    <span
                      className={[
                        "size-1.5 rounded-full",
                        r.status === "revert" ? "bg-[--color-alarm]" : "bg-[--color-ok]",
                      ].join(" ")}
                    />
                    {r.status === "revert" ? "status 0" : "status 1"}
                  </span>
                </TableCell>
                <TableCell className="py-1.5 text-right">
                  <TxLink hash={r.hash} />
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </div>
    </Card>
  );
}
