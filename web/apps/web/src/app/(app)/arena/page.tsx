"use client";

import { motion } from "framer-motion";

import { ArenaMemory } from "@/components/arena/arena-memory";
import { ArenaPlane } from "@/components/arena/arena-plane";
import { Leaderboard } from "@/components/arena/leaderboard";
import { LiveFeed } from "@/components/arena/live-feed";
import { StatusDot } from "@/components/panels/primitives";
import { useBlockNumber } from "@/lib/hooks";

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
            Autonomous ERC-8004 agents reason with compressed memory and duel on live prices —
            all on Arc testnet, read straight from the public RPC.
          </p>
        </header>

        {/* Hero: the living economy of proven agents. */}
        <ArenaPlane />

        {/* Compact 2-col: standings beside the live action feed. */}
        <div className="grid items-stretch gap-6 lg:grid-cols-2">
          <Leaderboard />
          <LiveFeed />
        </div>

        {/* Slim memory proof strip. */}
        <ArenaMemory />

        <footer className="border-t border-border/60 pt-4 text-[10px] text-muted-foreground">
          Arcane · reads Colosseum + AgentRegistry + PerformanceOracle + MemoryAnchor
          on Arc testnet (chain 5042002) via the public RPC. Read-only; wallet writes use your wallet.
        </footer>
      </motion.div>
    </main>
  );
}
