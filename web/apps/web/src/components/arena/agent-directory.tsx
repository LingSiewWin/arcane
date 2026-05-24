"use client";

import { Skeleton } from "@web/ui/components/skeleton";

import {
  proofFor,
  reputationFor,
  useAgents,
  useArenaProof,
  useReputation,
  REGISTRY_CONFIGURED,
} from "@/lib/arena";

import { PanelTitle } from "@/components/panels/primitives";

import { AgentCard } from "./agent-card";
import { ArenaEmpty } from "./arena-empty";

export function AgentDirectory() {
  const agents = useAgents();
  const reputation = useReputation();
  const proof = useArenaProof(agents.data);

  if (!REGISTRY_CONFIGURED) {
    return (
      <section className="flex flex-col gap-4">
        <PanelTitle index="01" title="Agent directory" subtitle="registry not configured" />
        <ArenaEmpty
          title="Registry not configured"
          cmd="NEXT_PUBLIC_AGENT_REGISTRY=0x… in web/apps/web/.env.local"
          hint="The address is read from the env at build time — it isn't hardcoded because the demo redeploys it."
        >
          Set <span className="font-mono">NEXT_PUBLIC_AGENT_REGISTRY</span> to the deployed
          AgentRegistry address, then restart the dev server. Until then the arena has nothing real
          to read — and never shows mock agents.
        </ArenaEmpty>
      </section>
    );
  }

  return (
    <section className="flex flex-col gap-4">
      <div className="flex items-end justify-between">
        <PanelTitle
          index="01"
          title="Agent directory"
          subtitle={
            agents.data
              ? proof.data
                ? `${[...proof.data.values()].filter((p) => p.proven).length} proven · ${agents.data.length} registered`
                : `${agents.data.length} registered`
              : "reading registry"
          }
        />
      </div>

      {agents.isPending ? (
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {Array.from({ length: 3 }).map((_, i) => (
            <Skeleton key={i} className="h-44 w-full" />
          ))}
        </div>
      ) : agents.isError ? (
        <ArenaEmpty title="Could not read the registry">
          The public Arc RPC rejected the directory read. Confirm{" "}
          <span className="font-mono">NEXT_PUBLIC_AGENT_REGISTRY</span> points at a contract on chain
          5042002.
        </ArenaEmpty>
      ) : agents.data && agents.data.length === 0 ? (
        <ArenaEmpty
          title="No agents registered yet"
          cmd="python -m agents.seed_alice  # or connect a wallet to register"
          hint="The directory reflects real on-chain registrations only."
        >
          Run the arena seeder to register real agents on Arc, or connect a wallet and use the
          Register tab to put your own identity on-chain.
        </ArenaEmpty>
      ) : (
        (() => {
          const all = agents.data ?? [];
          // Earned visibility: proven agents (bond + ≥1 action) lead the live
          // economy; registered-but-idle ghosts drop to a muted "pending" tier
          // so they never inflate the proof surface. While proof is still
          // loading we show everything as the live grid (no false "pending").
          const proofReady = proof.data !== undefined;
          const proven = proofReady
            ? all.filter((a) => proofFor(proof.data, a.agentId).proven)
            : all;
          const pending = proofReady
            ? all.filter((a) => !proofFor(proof.data, a.agentId).proven)
            : [];

          return (
            <div className="flex flex-col gap-6">
              {proven.length > 0 ? (
                <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
                  {proven.map((agent, i) => (
                    <AgentCard
                      key={agent.agentId}
                      agent={agent}
                      index={i}
                      reputation={reputationFor(reputation.data, agent.operator)}
                      proof={proofFor(proof.data, agent.agentId)}
                    />
                  ))}
                </div>
              ) : proofReady ? (
                <ArenaEmpty title="No proven agents yet">
                  Agents register openly, but the live economy only shows those that have posted a
                  bond and emitted at least one real on-chain action. Run the continuous runner (or
                  register and act) to earn a spot here.
                </ArenaEmpty>
              ) : null}

              {pending.length > 0 ? (
                <div className="flex flex-col gap-2">
                  <p className="text-[10px] uppercase tracking-wider text-muted-foreground">
                    Pending · registered, not yet proven ({pending.length})
                  </p>
                  <div className="grid gap-4 opacity-60 sm:grid-cols-2 lg:grid-cols-3">
                    {pending.map((agent, i) => (
                      <AgentCard
                        key={agent.agentId}
                        agent={agent}
                        index={i}
                        reputation={reputationFor(reputation.data, agent.operator)}
                        proof={proofFor(proof.data, agent.agentId)}
                      />
                    ))}
                  </div>
                </div>
              ) : null}
            </div>
          );
        })()
      )}
    </section>
  );
}
