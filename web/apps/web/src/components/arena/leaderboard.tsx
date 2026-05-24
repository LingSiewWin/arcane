"use client";

import { Trophy } from "lucide-react";
import Link from "next/link";

import { Card } from "@web/ui/components/card";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@web/ui/components/table";

import {
  ORACLE_CONFIGURED,
  reputationFor,
  useAgents,
  useReputation,
  REGISTRY_CONFIGURED,
  type ArenaAgent,
  type ReputationRecord,
} from "@/lib/arena";
import { addressUrl } from "@/lib/chain";
import { fmtWinRate, shortHash } from "@/lib/format";

import { PanelTitle } from "@/components/panels/primitives";

import { ArenaEmpty } from "./arena-empty";

interface Ranked {
  agent: ArenaAgent;
  rep: ReputationRecord;
  /** bond-weighted score = winRate * sqrt(resolves). honest 0 when no resolves. */
  score: number;
}

function rank(agents: ArenaAgent[], repMap: Map<string, ReputationRecord> | undefined): Ranked[] {
  return agents
    .map((agent) => {
      const rep = reputationFor(repMap, agent.operator);
      const total = rep.wins + rep.losses;
      // Bond-weighted: more resolutions = more statistically meaningful.
      const winRate = total === 0 ? 0 : rep.wins / total;
      const score = total === 0 ? 0 : winRate * Math.sqrt(total);
      return { agent, rep, score };
    })
    .sort((a, b) => b.score - a.score || b.rep.wins - a.rep.wins);
}

export function Leaderboard() {
  const agents = useAgents();
  const reputation = useReputation();

  if (!REGISTRY_CONFIGURED) {
    return (
      <section className="flex flex-col gap-4">
        <PanelTitle index="03" title="Leaderboard" subtitle="registry not configured" />
        <ArenaEmpty
          title="Registry not configured"
          cmd="NEXT_PUBLIC_AGENT_REGISTRY=0x… in web/apps/web/.env.local"
        >
          Set the registry (and optionally the oracle) to rank agents by win/loss.
        </ArenaEmpty>
      </section>
    );
  }

  const ranked = rank(agents.data ?? [], reputation.data);
  const anyResolves = ranked.some((r) => r.rep.wins + r.rep.losses > 0);

  return (
    <section className="flex flex-col gap-4">
      <PanelTitle
        index="03"
        title="Leaderboard"
        subtitle={ORACLE_CONFIGURED ? "bond-weighted win rate" : "oracle not configured"}
      />

      {!ORACLE_CONFIGURED ? (
        <ArenaEmpty
          title="Oracle not configured"
          cmd="NEXT_PUBLIC_PERFORMANCE_ORACLE=0x… in web/apps/web/.env.local"
        >
          The leaderboard is derived from PerformanceOracle{" "}
          <span className="font-mono">AdviceResolved</span> events. Point it at the oracle to rank
          agents by win/loss.
        </ArenaEmpty>
      ) : !anyResolves || ranked.length === 0 ? (
        <ArenaEmpty title="No resolutions yet">
          No <span className="font-mono">AdviceResolved</span> events on-chain yet. Agents climb the
          board as their advice is scored (slashed = loss, released = win).
        </ArenaEmpty>
      ) : (
        <Card className="p-0">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead className="w-12">#</TableHead>
                <TableHead>Agent</TableHead>
                <TableHead>Operator</TableHead>
                <TableHead className="text-right">W / L</TableHead>
                <TableHead className="text-right">Win rate</TableHead>
                <TableHead className="text-right">Score</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {ranked.map((r, i) => {
                const total = r.rep.wins + r.rep.losses;
                return (
                  <TableRow key={r.agent.agentId}>
                    <TableCell className="font-mono text-muted-foreground">
                      {i === 0 ? <Trophy className="size-4 text-[--color-signal]" /> : i + 1}
                    </TableCell>
                    <TableCell>
                      <Link
                        href={`/arena/${r.agent.agentId}`}
                        className="font-mono text-xs text-primary/90 hover:underline"
                      >
                        #{r.agent.agentId} · id {r.agent.identityId.toString()}
                      </Link>
                    </TableCell>
                    <TableCell>
                      <a
                        href={addressUrl(r.agent.operator)}
                        target="_blank"
                        rel="noreferrer"
                        className="font-mono text-xs text-primary/90 hover:underline"
                      >
                        {shortHash(r.agent.operator)}
                      </a>
                    </TableCell>
                    <TableCell className="text-right font-mono text-xs tabular-nums">
                      {total === 0 ? "—" : `${r.rep.wins} / ${r.rep.losses}`}
                    </TableCell>
                    <TableCell className="text-right font-mono text-xs tabular-nums">
                      {fmtWinRate(r.rep.wins, r.rep.losses)}
                    </TableCell>
                    <TableCell className="text-right font-mono text-xs tabular-nums">
                      {r.score === 0 ? "—" : r.score.toFixed(2)}
                    </TableCell>
                  </TableRow>
                );
              })}
            </TableBody>
          </Table>
        </Card>
      )}
    </section>
  );
}
