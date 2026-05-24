"use client";

import { motion } from "framer-motion";
import { ArrowUpRight, Fingerprint } from "lucide-react";
import Link from "next/link";

import { Badge } from "@web/ui/components/badge";
import { Card } from "@web/ui/components/card";

import { addressUrl } from "@/lib/chain";
import { fmtWinRate, shortHash } from "@/lib/format";
import type { AgentProof, ArenaAgent, ReputationRecord } from "@/lib/arena";

import { Mono, StatusDot } from "@/components/panels/primitives";

export function AgentCard({
  agent,
  reputation,
  index,
  proof,
}: {
  agent: ArenaAgent;
  reputation: ReputationRecord;
  index: number;
  proof?: AgentProof;
}) {
  const total = reputation.wins + reputation.losses;
  return (
    <motion.div
      initial={{ opacity: 0, y: 12 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.35, delay: Math.min(index * 0.04, 0.4), ease: [0.16, 1, 0.3, 1] }}
    >
      <Link href={`/arena/${agent.agentId}`} className="group block h-full">
        <Card className="flex h-full flex-col gap-3 p-4 transition-colors hover:border-primary/40">
          <div className="flex items-start justify-between gap-2">
            <div className="flex items-center gap-2">
              <span className="flex size-7 items-center justify-center rounded-md bg-primary/12 font-mono text-xs text-primary">
                #{agent.agentId}
              </span>
              <div className="flex flex-col leading-tight">
                <span className="font-mono text-xs">identity #{agent.identityId.toString()}</span>
                <span className="text-[10px] text-muted-foreground">agent {agent.agentId}</span>
              </div>
            </div>
            <div className="flex items-center gap-1.5">
              <StatusDot
                tone={agent.active ? "ok" : "idle"}
                label={agent.active ? "active" : "inactive"}
              />
              <span className="text-[10px] uppercase tracking-wider text-muted-foreground">
                {agent.active ? "active" : "inactive"}
              </span>
            </div>
          </div>

          <div className="grid grid-cols-2 gap-3 text-xs">
            <div className="flex flex-col gap-0.5">
              <span className="text-[10px] uppercase tracking-wider text-muted-foreground">
                operator
              </span>
              <button
                type="button"
                onClick={(e) => {
                  e.preventDefault();
                  e.stopPropagation();
                  window.open(addressUrl(agent.operator), "_blank", "noreferrer");
                }}
                className="text-left font-mono text-primary/90 hover:underline"
              >
                {shortHash(agent.operator)}
              </button>
            </div>
            <div className="flex flex-col gap-0.5">
              <span className="text-[10px] uppercase tracking-wider text-muted-foreground">
                constitution
              </span>
              <Mono title={agent.constitutionHash}>{shortHash(agent.constitutionHash)}</Mono>
            </div>
            <div className="flex flex-col gap-0.5">
              <span className="text-[10px] uppercase tracking-wider text-muted-foreground">
                bond vault
              </span>
              <button
                type="button"
                onClick={(e) => {
                  e.preventDefault();
                  e.stopPropagation();
                  window.open(addressUrl(agent.bondVault), "_blank", "noreferrer");
                }}
                className="text-left font-mono text-primary/90 hover:underline"
              >
                {shortHash(agent.bondVault)}
              </button>
            </div>
            <div className="flex flex-col gap-0.5">
              <span className="text-[10px] uppercase tracking-wider text-muted-foreground">
                reputation
              </span>
              {total === 0 ? (
                <span className="font-mono text-muted-foreground">no resolves</span>
              ) : (
                <span className="font-mono tabular-nums">
                  {fmtWinRate(reputation.wins, reputation.losses)}{" "}
                  <span className="text-muted-foreground">
                    ({reputation.wins}W/{reputation.losses}L)
                  </span>
                </span>
              )}
            </div>
          </div>

          <div className="mt-auto flex items-center justify-between border-t border-border/50 pt-2">
            <div className="flex items-center gap-1.5">
              {agent.darkPoolUrl ? (
                <Badge variant="outline" className="gap-1 font-mono text-[10px]">
                  <Fingerprint className="size-3" />
                  dark pool
                </Badge>
              ) : (
                <span className="text-[10px] text-muted-foreground">no dark pool url</span>
              )}
              {proof ? (
                <span
                  title={
                    proof.proven
                      ? `proven: ${proof.actionCount} on-chain actions, bond posted`
                      : "registered but not yet proven (needs a posted bond + ≥1 action)"
                  }
                  className={`inline-flex items-center gap-1 rounded border px-1.5 py-0.5 font-mono text-[9px] uppercase tracking-wide ${
                    proof.proven
                      ? "border-[--color-ok]/40 text-[--color-ok]"
                      : "border-border/60 text-muted-foreground"
                  }`}
                >
                  {proof.proven ? "proven" : "pending"} · {proof.actionCount}
                </span>
              ) : null}
            </div>
            <span className="inline-flex items-center gap-1 text-[10px] text-muted-foreground transition-colors group-hover:text-primary">
              profile
              <ArrowUpRight className="size-3" />
            </span>
          </div>
        </Card>
      </Link>
    </motion.div>
  );
}
