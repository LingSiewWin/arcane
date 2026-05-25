"use client";

import { Shield, Trophy } from "lucide-react";

import { Card } from "@web/ui/components/card";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@web/ui/components/table";

import { COLOSSEUM_CONFIGURED, useArenaStandings, type ArenaStanding } from "@/lib/colosseum";
import { addressUrl } from "@/lib/chain";
import { shortHash } from "@/lib/format";

import { PanelTitle } from "@/components/panels/primitives";

import { ArenaEmpty } from "./arena-empty";

/** Signed basis-points, e.g. "+12 bps" / "−5 bps". */
function fmtAlpha(bps: number): string {
  const sign = bps > 0 ? "+" : bps < 0 ? "−" : "";
  return `${sign}${Math.abs(bps).toLocaleString("en-US")} bps`;
}

/** survived/ingested, honest "—" when nothing was ingested. */
function fmtResilience(s: ArenaStanding): string {
  if (s.ingested === 0) return "—";
  return `${s.survived}/${s.ingested}`;
}

function AddressLink({ address }: { address: string }) {
  return (
    <a
      href={addressUrl(address)}
      target="_blank"
      rel="noreferrer"
      className="font-mono text-xs text-primary/90 hover:underline"
    >
      {shortHash(address)}
    </a>
  );
}

/** A single ranking table (Alpha or Iron Shield) over the standings. */
function RankingCard({
  standings,
  metric,
}: {
  standings: ArenaStanding[];
  metric: "alpha" | "shield";
}) {
  return (
    <Card className="p-0">
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead className="w-12">#</TableHead>
            <TableHead>Agent</TableHead>
            <TableHead className="text-right">Alpha</TableHead>
            <TableHead className="text-right">Iron Shield</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {standings.map((s, i) => (
            <TableRow key={s.address}>
              <TableCell className="font-mono text-muted-foreground">
                {i === 0 ? (
                  metric === "alpha" ? (
                    <Trophy className="size-4 text-[--color-signal]" />
                  ) : (
                    <Shield className="size-4 text-[--color-signal]" />
                  )
                ) : (
                  i + 1
                )}
              </TableCell>
              <TableCell>
                <AddressLink address={s.address} />
              </TableCell>
              <TableCell
                className={`text-right font-mono text-xs tabular-nums ${
                  s.alphaBps > 0
                    ? "text-[--color-ok]"
                    : s.alphaBps < 0
                      ? "text-[--color-alarm]"
                      : "text-muted-foreground"
                }`}
              >
                {fmtAlpha(s.alphaBps)}
              </TableCell>
              <TableCell className="text-right font-mono text-xs tabular-nums">
                {fmtResilience(s)}
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </Card>
  );
}

export function Leaderboard() {
  const standings = useArenaStandings();

  if (!COLOSSEUM_CONFIGURED) {
    return (
      <section className="flex flex-col gap-4">
        <PanelTitle index="03" title="Arena Standings" subtitle="colosseum not configured" />
        <ArenaEmpty
          title="Colosseum not configured"
          cmd="NEXT_PUBLIC_COLOSSEUM=0x… in web/apps/web/.env.local"
        >
          Standings are derived from Colosseum{" "}
          <span className="font-mono">CallReported</span> events. Point the app at the Colosseum to
          rank agents by Alpha (cumulative PnL) and Iron Shield (manipulation resilience).
        </ArenaEmpty>
      </section>
    );
  }

  const data = standings.data ?? [];

  if (data.length === 0) {
    return (
      <section className="flex flex-col gap-4">
        <PanelTitle index="03" title="Arena Standings" subtitle="alpha · iron shield" />
        <ArenaEmpty title="No scored calls yet — start an arena">
          No <span className="font-mono">CallReported</span> events on-chain yet. Agents enter the
          standings the moment a duel scores their first trading call.
        </ArenaEmpty>
      </section>
    );
  }

  // Alpha: cumulative PnL desc. Iron Shield: resilience desc, alpha tie-break.
  const byAlpha = [...data].sort((a, b) => b.alphaBps - a.alphaBps);
  const byShield = [...data].sort(
    (a, b) => b.resilience - a.resilience || b.alphaBps - a.alphaBps,
  );

  return (
    <section className="flex flex-col gap-6">
      <div className="flex flex-col gap-4">
        <PanelTitle
          index="03"
          title="Alpha"
          subtitle="cumulative PnL — sum of scored calls (bps)"
        />
        <RankingCard standings={byAlpha} metric="alpha" />
      </div>

      <div className="flex flex-col gap-4">
        <PanelTitle
          index="04"
          title="Iron Shield"
          subtitle="manipulation resilience — injections survived / ingested"
        />
        <RankingCard standings={byShield} metric="shield" />
      </div>
    </section>
  );
}
