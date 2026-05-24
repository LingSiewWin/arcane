"use client";

import { motion } from "framer-motion";
import { ArrowLeft } from "lucide-react";
import Link from "next/link";
import { useParams } from "next/navigation";
import { useMemo } from "react";

import { Badge } from "@web/ui/components/badge";
import { Card } from "@web/ui/components/card";
import { Separator } from "@web/ui/components/separator";
import { Skeleton } from "@web/ui/components/skeleton";

import { ArenaEmpty } from "@/components/arena/arena-empty";
import { Mono, PanelTitle, StatusDot, TxLink } from "@/components/panels/primitives";
import {
  reputationFor,
  useAgent,
  useAgentActions,
  useIdentityOwnerByRegistry,
  useReputation,
  REGISTRY_CONFIGURED,
  type FeedEvent,
} from "@/lib/arena";
import { addressUrl } from "@/lib/chain";
import { ACTION_KINDS, ERC8004_IDENTITY_REGISTRY } from "@/lib/constants";
import { fmtTime, fmtWinRate, relativeTime, shortHash } from "@/lib/format";

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex flex-col gap-0.5">
      <span className="text-[10px] uppercase tracking-wider text-muted-foreground">{label}</span>
      <span className="text-sm">{children}</span>
    </div>
  );
}

function ActionList({ events, emptyLabel }: { events: FeedEvent[]; emptyLabel: string }) {
  if (events.length === 0) {
    return <p className="py-3 text-xs text-muted-foreground">{emptyLabel}</p>;
  }
  return (
    <ul className="flex flex-col gap-2">
      {events.map((e) => {
        const meta = ACTION_KINDS[e.kind];
        return (
          <li
            key={e.id}
            className="flex items-center justify-between gap-3 rounded-md border border-border/50 bg-card/40 px-3 py-2"
          >
            <div className="flex flex-col leading-tight">
              <span className="text-xs capitalize">{meta.label}</span>
              <span className="font-mono text-[10px] text-muted-foreground">
                {relativeTime(e.timestamp)} · block {e.blockNumber.toString()}
              </span>
            </div>
            <TxLink hash={e.txHash} label="tx" />
          </li>
        );
      })}
    </ul>
  );
}

export default function AgentProfilePage() {
  const params = useParams<{ agentId: string }>();
  // agentId is 1-indexed on-chain (id 0 == "no agent"); reject 0 and non-ints.
  const parsedId = /^\d+$/.test(params.agentId ?? "") ? Number(params.agentId) : undefined;
  const agentId = parsedId !== undefined && parsedId >= 1 ? parsedId : undefined;

  const agent = useAgent(agentId);
  const actions = useAgentActions(agentId);
  const reputation = useReputation();
  const owner = useIdentityOwnerByRegistry(ERC8004_IDENTITY_REGISTRY, agent.data?.identityId);

  const grouped = useMemo(() => {
    const all = actions.data ?? [];
    return {
      advice: all.filter((a) => a.kind === 0),
      queries: all.filter((a) => a.kind === 1),
      bond: all.filter((a) => a.kind === 3 || a.kind === 4),
      reverts: all.filter((a) => a.kind === 2),
    };
  }, [actions.data]);

  const rep = reputationFor(reputation.data, agent.data?.operator ?? "0x0000000000000000000000000000000000000000");

  return (
    <main className="mx-auto w-full max-w-5xl px-4 py-6 sm:px-6">
      <motion.div
        initial={{ opacity: 0, y: 8 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.35, ease: [0.16, 1, 0.3, 1] }}
        className="flex flex-col gap-6"
      >
        <Link
          href="/arena"
          className="inline-flex w-fit items-center gap-1.5 text-xs text-muted-foreground transition-colors hover:text-foreground"
        >
          <ArrowLeft className="size-3.5" />
          back to arena
        </Link>

        {!REGISTRY_CONFIGURED ? (
          <ArenaEmpty
            title="Registry not configured"
            cmd="NEXT_PUBLIC_AGENT_REGISTRY=0x… in web/apps/web/.env.local"
          >
            Set the registry address to view agent profiles.
          </ArenaEmpty>
        ) : agentId === undefined ? (
          <ArenaEmpty title="Invalid agent id">
            The agent id in the URL must be a positive integer (agents are 1-indexed
            on-chain).
          </ArenaEmpty>
        ) : agent.isPending ? (
          <div className="flex flex-col gap-6">
            <Skeleton className="h-40 w-full" />
            <Skeleton className="h-48 w-full" />
          </div>
        ) : agent.isError ? (
          <ArenaEmpty title="Agent not found">
            No agent #{agentId} in the registry on chain 5042002.
          </ArenaEmpty>
        ) : agent.data ? (
          <>
            <Card className="flex flex-col gap-4 p-5">
              <div className="flex items-center justify-between">
                <div className="flex items-center gap-3">
                  <span className="flex size-9 items-center justify-center rounded-md bg-primary/12 font-mono text-sm text-primary">
                    #{agent.data.agentId}
                  </span>
                  <div>
                    <h1 className="text-base font-semibold">
                      Agent #{agent.data.agentId}
                    </h1>
                    <span className="font-mono text-xs text-muted-foreground">
                      identity #{agent.data.identityId.toString()}
                    </span>
                  </div>
                </div>
                <Badge variant={agent.data.active ? "default" : "secondary"} className="gap-1.5">
                  <StatusDot
                    tone={agent.data.active ? "ok" : "idle"}
                    label={agent.data.active ? "active" : "inactive"}
                  />
                  {agent.data.active ? "active" : "inactive"}
                </Badge>
              </div>

              <Separator />

              <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
                <Field label="operator">
                  <a
                    href={addressUrl(agent.data.operator)}
                    target="_blank"
                    rel="noreferrer"
                    className="font-mono text-xs text-primary/90 hover:underline"
                  >
                    {shortHash(agent.data.operator)}
                  </a>
                </Field>
                <Field label="identity owner (live)">
                  {owner.isPending ? (
                    <span className="font-mono text-xs text-muted-foreground">reading…</span>
                  ) : owner.data ? (
                    <a
                      href={addressUrl(owner.data)}
                      target="_blank"
                      rel="noreferrer"
                      className="font-mono text-xs text-primary/90 hover:underline"
                    >
                      {shortHash(owner.data)}
                    </a>
                  ) : (
                    <span className="font-mono text-xs text-muted-foreground">—</span>
                  )}
                </Field>
                <Field label="registered">
                  <span className="font-mono text-xs">{fmtTime(agent.data.registeredAt)}</span>
                </Field>
                <Field label="constitution hash">
                  <Mono title={agent.data.constitutionHash}>
                    {shortHash(agent.data.constitutionHash, 10, 8)}
                  </Mono>
                </Field>
                <Field label="bond vault">
                  <a
                    href={addressUrl(agent.data.bondVault)}
                    target="_blank"
                    rel="noreferrer"
                    className="font-mono text-xs text-primary/90 hover:underline"
                  >
                    {shortHash(agent.data.bondVault)}
                  </a>
                </Field>
                <Field label="reputation">
                  {rep.wins + rep.losses === 0 ? (
                    <span className="font-mono text-xs text-muted-foreground">no resolves</span>
                  ) : (
                    <span className="font-mono text-xs tabular-nums">
                      {fmtWinRate(rep.wins, rep.losses)} · {rep.wins}W / {rep.losses}L
                    </span>
                  )}
                </Field>
              </div>

              {agent.data.darkPoolUrl ? (
                <Field label="dark pool url">
                  <a
                    href={agent.data.darkPoolUrl}
                    target="_blank"
                    rel="noreferrer"
                    className="break-all font-mono text-xs text-primary/90 hover:underline"
                  >
                    {agent.data.darkPoolUrl}
                  </a>
                </Field>
              ) : null}
            </Card>

            <div className="grid gap-6 lg:grid-cols-2">
              <Card className="flex flex-col gap-3 p-5">
                <PanelTitle title="Advice track record" subtitle="kind 0 — advice published" />
                {actions.isPending ? (
                  <Skeleton className="h-20 w-full" />
                ) : (
                  <ActionList events={grouped.advice} emptyLabel="No advice published yet." />
                )}
              </Card>

              <Card className="flex flex-col gap-3 p-5">
                <PanelTitle title="Bond & slash history" subtitle="kinds 3/4 — slashed / released" />
                {actions.isPending ? (
                  <Skeleton className="h-20 w-full" />
                ) : (
                  <ActionList
                    events={grouped.bond}
                    emptyLabel="No bond slashes or releases yet."
                  />
                )}
              </Card>

              <Card className="flex flex-col gap-3 p-5">
                <PanelTitle title="Queries paid" subtitle="kind 1 — query paid" />
                {actions.isPending ? (
                  <Skeleton className="h-20 w-full" />
                ) : (
                  <ActionList events={grouped.queries} emptyLabel="No paid queries yet." />
                )}
              </Card>

              <Card className="flex flex-col gap-3 p-5">
                <PanelTitle title="Constitution reverts" subtitle="kind 2 — bounded by the constitution" />
                {actions.isPending ? (
                  <Skeleton className="h-20 w-full" />
                ) : (
                  <ActionList events={grouped.reverts} emptyLabel="No constitution reverts." />
                )}
              </Card>
            </div>
          </>
        ) : null}
      </motion.div>
    </main>
  );
}
