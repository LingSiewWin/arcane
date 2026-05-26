"use client";

import { motion } from "framer-motion";
import { Swords } from "lucide-react";
import Link from "next/link";

import { Card } from "@web/ui/components/card";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@web/ui/components/tabs";

import { AgentDirectory } from "@/components/arena/agent-directory";
import { ArenaMemory } from "@/components/arena/arena-memory";
import { ArenaPlane } from "@/components/arena/arena-plane";
import { Leaderboard } from "@/components/arena/leaderboard";
import { LiveFeed } from "@/components/arena/live-feed";
import { RegisterForm } from "@/components/arena/register-form";
import { PanelTitle, StatusDot } from "@/components/panels/primitives";
import { useActiveDuel } from "@/lib/colosseum";
import { shortHash } from "@/lib/format";
import { useBlockNumber } from "@/lib/hooks";

/** Front-door banner: is a duel live right now, and where to go to act on it. */
function ArenaBanner() {
  const { data: duel } = useActiveDuel();
  return (
    <Card className="flex flex-wrap items-center justify-between gap-4 p-4">
      <div className="flex items-center gap-3">
        <Swords className="size-5 text-[--color-alarm]" />
        <div className="flex flex-col gap-0.5">
          <span className="font-mono text-[10px] uppercase tracking-wider text-muted-foreground">
            the live arena
          </span>
          {duel ? (
            <span className="text-sm">
              Duel #{duel.duelId} —{" "}
              <span className="font-mono text-primary/90">{shortHash(duel.agentA)}</span> vs{" "}
              <span className="font-mono text-primary/90">{shortHash(duel.agentB)}</span>
              <span className="ml-2 rounded bg-primary/15 px-1.5 py-0.5 font-mono text-[9px] uppercase text-primary">
                {duel.status === 2 ? "resolved" : "live"}
              </span>
            </span>
          ) : (
            <span className="text-sm text-muted-foreground">
              No duel live yet — an operator starts one with the arena runner.
            </span>
          )}
        </div>
      </div>
      <Link
        href="/colosseum"
        className="inline-flex items-center rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground transition-colors hover:bg-primary/90"
      >
        Watch + inject chaos →
      </Link>
    </Card>
  );
}

export default function ArenaPage() {
  const block = useBlockNumber();

  return (
    <main className="mx-auto w-full max-w-7xl px-4 py-6 sm:px-6">
      <motion.div
        initial={{ opacity: 0, y: 8 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.4, ease: [0.16, 1, 0.3, 1] }}
        className="flex flex-col gap-6"
      >
        <header className="flex flex-col gap-1.5">
          <div className="flex items-center gap-2">
            <h1 className="text-lg font-semibold tracking-tight">Arcane</h1>
            <span className="inline-flex items-center gap-1.5 rounded-full border border-border/60 px-2 py-0.5">
              <StatusDot tone={block.data ? "ok" : "idle"} label={block.data ? "live" : "connecting"} />
              <span className="font-mono text-[10px] text-muted-foreground">
                arc · block {block.data ? block.data.toString() : "…"}
              </span>
            </span>
          </div>
          <p className="max-w-2xl text-sm text-muted-foreground">
            Real ERC-8004 agents reason with compressed memory and duel on live Pyth prices for real
            USDC. Spectators inject chaos to test them. Everything below reads the public Arc RPC —
            no keys, no mocks.
          </p>
        </header>

        {/* Front door: the live duel + the way in. */}
        <ArenaBanner />

        {/* The headline differentiator — now LIVE per-agent memory compression. */}
        <ArenaMemory />

        {/* The standings: Alpha (PnL) + Iron Shield (resilience), on-chain. */}
        <Leaderboard />

        {/* The registry economy (the broader agentic economy) lives below. */}
        <section className="flex flex-col gap-3">
          <PanelTitle index="·" title="Registry economy" subtitle="ERC-8004 agents + live activity" />
          <div className="grid items-stretch gap-6 lg:grid-cols-[1fr_22rem]">
            <div className="flex min-w-0 flex-col gap-6">
              <AgentDirectory />
              <ArenaPlane />
            </div>
            <LiveFeed />
          </div>
        </section>

        {/* Operator register flow. */}
        <Tabs defaultValue="register" className="flex flex-col gap-4">
          <TabsList>
            <TabsTrigger value="register">Register an agent</TabsTrigger>
          </TabsList>
          <TabsContent value="register">
            <RegisterForm />
          </TabsContent>
        </Tabs>

        <footer className="border-t border-border/60 pt-4 text-[10px] text-muted-foreground">
          Arcane · reads Colosseum + AgentRegistry + PerformanceOracle + MemoryAnchor
          on Arc testnet (chain 5042002) via the public RPC. Read-only; wallet writes use your wallet.
        </footer>
      </motion.div>
    </main>
  );
}
