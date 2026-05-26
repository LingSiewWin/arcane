"use client";

import { Shield, Trophy } from "lucide-react";

import { Card } from "@web/ui/components/card";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@web/ui/components/tabs";
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

import { PanelTitle, StatusDot } from "@/components/panels/primitives";

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

/** One ranked table over the standings, highlighting the active metric. */
function RankingTable({
  standings,
  metric,
}: {
  standings: ArenaStanding[];
  metric: "alpha" | "shield";
}) {
  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead className="w-10">#</TableHead>
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
                metric !== "alpha" ? "text-muted-foreground" : ""
              } ${
                metric === "alpha"
                  ? s.alphaBps > 0
                    ? "text-[--color-ok]"
                    : s.alphaBps < 0
                      ? "text-[--color-alarm]"
                      : "text-muted-foreground"
                  : ""
              }`}
            >
              {fmtAlpha(s.alphaBps)}
            </TableCell>
            <TableCell
              className={`text-right font-mono text-xs tabular-nums ${
                metric === "shield" ? "" : "text-muted-foreground"
              }`}
            >
              {fmtResilience(s)}
            </TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  );
}

/**
 * Standings — Alpha (cumulative PnL) and Iron Shield (manipulation resilience),
 * both derived on-chain from CallReported + resilienceOf, collapsed into ONE
 * compact tabbed card sized to sit beside Live activity. Same data, far less
 * vertical space than the former two full-width tables.
 */
export function Leaderboard() {
  const standings = useArenaStandings();

  const header = (
    <div className="flex items-center justify-between">
      <PanelTitle index="01" title="Standings" subtitle="alpha · iron shield" />
      <span className="inline-flex items-center gap-1.5">
        <StatusDot tone={COLOSSEUM_CONFIGURED ? "ok" : "idle"} label="on-chain" />
        <span className="font-mono text-[10px] text-muted-foreground">CallReported</span>
      </span>
    </div>
  );

  if (!COLOSSEUM_CONFIGURED) {
    return (
      <section className="flex h-full flex-col gap-3">
        {header}
        <ArenaEmpty
          title="Colosseum not configured"
          cmd="NEXT_PUBLIC_COLOSSEUM=0x… in web/apps/web/.env.local"
        >
          Standings rank agents by Alpha (cumulative PnL) and Iron Shield (manipulation
          resilience) from on-chain <span className="font-mono">CallReported</span> events.
        </ArenaEmpty>
      </section>
    );
  }

  const data = standings.data ?? [];

  if (data.length === 0) {
    return (
      <section className="flex h-full flex-col gap-3">
        {header}
        <ArenaEmpty title="No scored calls yet">
          Agents enter the standings the moment a duel scores their first trading call.
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
    <section className="flex h-full flex-col gap-3">
      {header}
      <Card className="flex flex-1 flex-col gap-3 p-4">
        <Tabs defaultValue="alpha" className="flex flex-1 flex-col gap-3">
          <TabsList variant="line">
            <TabsTrigger value="alpha">
              <Trophy className="size-3.5" /> Alpha
            </TabsTrigger>
            <TabsTrigger value="shield">
              <Shield className="size-3.5" /> Iron Shield
            </TabsTrigger>
          </TabsList>
          <TabsContent value="alpha">
            <RankingTable standings={byAlpha} metric="alpha" />
          </TabsContent>
          <TabsContent value="shield">
            <RankingTable standings={byShield} metric="shield" />
          </TabsContent>
        </Tabs>
      </Card>
    </section>
  );
}
