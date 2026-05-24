"use client";

/**
 * Arena data layer — all reads go through the PUBLIC Arc RPC (see chain.ts).
 * No keys, no private RPC. Directory + leaderboard are TanStack queries;
 * the live AgentAction feed is a viem watch subscription (polling fallback).
 */

import { useQuery } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { decodeAbiParameters, type Address, type Hex } from "viem";

import { agentRegistryAbi, bondVaultAbi, performanceOracleAbi } from "@/lib/abis";
import { publicClient } from "@/lib/chain";
import {
  AGENT_REGISTRY_ADDRESS,
  PERFORMANCE_ORACLE_ADDRESS,
  isConfiguredAddress,
  type ActionKind,
} from "@/lib/constants";

/* -------------------------------- types --------------------------------- */

export interface ArenaAgent {
  agentId: number;
  identityId: bigint;
  constitutionHash: Hex;
  bondVault: Address;
  darkPoolUrl: string;
  operator: Address;
  registeredAt: bigint;
  active: boolean;
}

/**
 * The decoded invocation trace carried in an AgentAction `payload`. Every field
 * here is read straight from the on-chain event — nothing is fetched off-chain.
 *  - `advice` (kind 0): the full reasoning + asset + stance + commitment hash.
 *  - `resolveBps` (kinds 3/4): the realised return in basis points.
 *  - legacy/empty payloads decode to all-undefined (the row still shows the hash).
 */
export interface ActionTrace {
  advice?: {
    reasoning: string;
    symbol: string;
    stance: string;
    adviceHash: Hex;
  };
  resolveBps?: number;
}

export interface FeedEvent {
  /** stable key: blockNumber:logIndex */
  id: string;
  agentId: number;
  kind: ActionKind;
  payload: Hex;
  timestamp: bigint;
  txHash: Hex;
  blockNumber: bigint;
  logIndex: number;
  /** Decoded from `payload` by kind — the verifiable invocation trace. */
  trace: ActionTrace;
}

const ADVICE_PAYLOAD_ABI = [
  { type: "string" },
  { type: "string" },
  { type: "string" },
  { type: "bytes32" },
] as const;

/**
 * Decode an AgentAction payload into its trace, dispatching on `kind`. Pure and
 * defensive: any malformed/legacy payload yields an empty trace rather than
 * throwing, so one bad row never breaks the feed.
 */
export function decodeActionTrace(kind: ActionKind, payload: Hex): ActionTrace {
  try {
    if (kind === 0) {
      // ADVICE_PUBLISHED: abi.encode(reasoning, symbol, stance, adviceHash).
      // A legacy bare keccak hash is 32 bytes (66 hex chars) and won't decode.
      if (payload.length <= 66) return {};
      const [reasoning, symbol, stance, adviceHash] = decodeAbiParameters(
        ADVICE_PAYLOAD_ABI,
        payload,
      );
      return { advice: { reasoning, symbol, stance, adviceHash } };
    }
    if (kind === 3 || kind === 4) {
      // BOND_SLASHED / BOND_RELEASED: abi.encode(int256 r_bps).
      const [bps] = decodeAbiParameters([{ type: "int256" }] as const, payload);
      return { resolveBps: Number(bps) };
    }
  } catch {
    // Malformed / legacy payload — fall through to an empty trace.
  }
  return {};
}

export interface ReputationRecord {
  operator: Address;
  wins: number;
  losses: number;
}

/** Whether the registry env var names a real address. Drives empty states. */
export const REGISTRY_CONFIGURED = isConfiguredAddress(AGENT_REGISTRY_ADDRESS);
export const ORACLE_CONFIGURED = isConfiguredAddress(PERFORMANCE_ORACLE_ADDRESS);
export const REGISTRY = AGENT_REGISTRY_ADDRESS as Address;
export const ORACLE = PERFORMANCE_ORACLE_ADDRESS as Address;

/**
 * The public Arc RPC caps eth_getLogs at a 10,000-block range (returns 413
 * beyond it). This walks [head - lookback, head] in <=CHUNK windows and
 * concatenates the decoded logs, so a wide history scan works against the
 * public node. A failed window is skipped (honest partial > a thrown scan).
 */
const GETLOGS_MAX_RANGE = BigInt(9_000);

async function getContractEventsChunked(params: {
  address: Address;
  abi: typeof agentRegistryAbi | typeof performanceOracleAbi;
  eventName: string;
  args?: Record<string, unknown>;
  lookback: bigint;
}): Promise<unknown[]> {
  const head = await publicClient.getBlockNumber();
  const start = head > params.lookback ? head - params.lookback : BigInt(0);

  // Build all <=10k windows up front, then fetch them CONCURRENTLY. Sequential
  // chunking made a wide scan take ~12 round-trips serially (seconds of dead
  // UI); the public RPC handles the parallel burst fine.
  const ranges: Array<[bigint, bigint]> = [];
  let from = start;
  while (from <= head) {
    const to = from + GETLOGS_MAX_RANGE < head ? from + GETLOGS_MAX_RANGE : head;
    ranges.push([from, to]);
    if (to >= head) break;
    from = to + BigInt(1);
  }

  const batches = await Promise.all(
    ranges.map(([f, t]) =>
      publicClient
        .getContractEvents({
          address: params.address,
          abi: params.abi as typeof agentRegistryAbi,
          eventName: params.eventName as "AgentAction",
          ...(params.args ? { args: params.args } : {}),
          fromBlock: f,
          toBlock: t,
        })
        .catch(() => [] as unknown[]), // a bad window contributes nothing
    ),
  );
  return batches.flat();
}

/* ----------------------------- directory read --------------------------- */

async function fetchAgent(agentId: number): Promise<ArenaAgent> {
  const a = (await publicClient.readContract({
    address: REGISTRY,
    abi: agentRegistryAbi,
    functionName: "getAgent",
    args: [BigInt(agentId)],
  })) as {
    identityId: bigint;
    constitutionHash: Hex;
    bondVault: Address;
    darkPoolUrl: string;
    operator: Address;
    registeredAt: bigint;
    active: boolean;
  };
  return { agentId, ...a };
}

/**
 * Reads agentCount() then getAgent(1..n). agentId is 1-INDEXED on-chain
 * (AgentRegistry: `agentId = ++_agentCount`, id 0 == "no agent" and reverts).
 * Honest [] when not configured.
 */
export function useAgents() {
  return useQuery<ArenaAgent[]>({
    queryKey: ["arena-agents", AGENT_REGISTRY_ADDRESS],
    enabled: REGISTRY_CONFIGURED,
    queryFn: async () => {
      const count = (await publicClient.readContract({
        address: REGISTRY,
        abi: agentRegistryAbi,
        functionName: "agentCount",
      })) as bigint;
      const n = Number(count);
      if (n === 0) return [];
      const ids = Array.from({ length: n }, (_, i) => i + 1);
      return Promise.all(ids.map(fetchAgent));
    },
    refetchInterval: 15_000,
  });
}

/** Single agent for the profile route. */
export function useAgent(agentId: number | undefined) {
  return useQuery<ArenaAgent>({
    queryKey: ["arena-agent", AGENT_REGISTRY_ADDRESS, agentId],
    // agentId is 1-indexed on-chain; id 0 reverts (AgentDoesNotExist).
    enabled: REGISTRY_CONFIGURED && agentId !== undefined && agentId >= 1,
    queryFn: () => fetchAgent(agentId as number),
    refetchInterval: 20_000,
  });
}

/** Live ownerOf() for a profile's identity NFT — proves current ownership. */
export function useIdentityOwnerByRegistry(identityRegistry: Address, identityId: bigint | undefined) {
  return useQuery<Address>({
    queryKey: ["identity-owner-live", identityRegistry, identityId?.toString()],
    enabled: identityId !== undefined,
    queryFn: () =>
      publicClient.readContract({
        address: identityRegistry,
        abi: [
          {
            type: "function",
            name: "ownerOf",
            stateMutability: "view",
            inputs: [{ name: "tokenId", type: "uint256" }],
            outputs: [{ name: "", type: "address" }],
          },
        ] as const,
        functionName: "ownerOf",
        args: [identityId as bigint],
      }) as Promise<Address>,
    refetchInterval: 30_000,
  });
}

/* ------------------------- live AgentAction feed ------------------------- */

/**
 * Streams AgentAction events from the registry.
 *
 * Mechanism: viem `publicClient.watchContractEvent` with `poll: true` and a
 * ~4s pollingInterval. Public Arc RPC does not guarantee eth_newFilter /
 * websockets, so polling getLogs is the robust path; viem dedupes and tracks
 * the from-block cursor for us. We also seed with a backfill of recent logs so
 * the feed isn't empty on first paint. Newest events are unshifted to the top.
 *
 * cap caps the in-memory list so a long-running session stays bounded.
 */
export function useLiveFeed(cap = 60) {
  const [events, setEvents] = useState<FeedEvent[]>([]);
  const seen = useRef<Set<string>>(new Set());

  useEffect(() => {
    if (!REGISTRY_CONFIGURED) return;
    let cancelled = false;

    const ingest = (incoming: FeedEvent[]) => {
      if (cancelled || incoming.length === 0) return;
      const fresh = incoming.filter((e) => !seen.current.has(e.id));
      if (fresh.length === 0) return;
      for (const e of fresh) seen.current.add(e.id);
      // newest first within the batch, then prepend
      fresh.sort((a, b) =>
        a.blockNumber === b.blockNumber
          ? b.logIndex - a.logIndex
          : Number(b.blockNumber - a.blockNumber),
      );
      setEvents((prev) => [...fresh, ...prev].slice(0, cap));
    };

    // Backfill: scan recent history in <=10k-block chunks (the public RPC's
    // getLogs cap) so the feed shows past actions even when no runner is live.
    (async () => {
      try {
        const logs = (await getContractEventsChunked({
          address: REGISTRY,
          abi: agentRegistryAbi,
          eventName: "AgentAction",
          lookback: BigInt(100_000),
        })) as RawActionLog[];
        ingest(logs.map(toFeedEvent));
      } catch {
        // The watch below still streams new events even if backfill fails.
      }
    })();

    // Live stream — viem polls getLogs and decodes each AgentAction for us.
    const unwatch = publicClient.watchContractEvent({
      address: REGISTRY,
      abi: agentRegistryAbi,
      eventName: "AgentAction",
      poll: true,
      pollingInterval: 4_000,
      onLogs: (logs) => ingest(logs.map(toFeedEvent)),
    });

    return () => {
      cancelled = true;
      unwatch();
    };
  }, [cap]);

  return events;
}

interface RawActionLog {
  args: { agentId?: bigint; kind?: number; payload?: Hex; timestamp?: bigint };
  transactionHash: Hex | null;
  blockNumber: bigint | null;
  logIndex: number | null;
}

function toFeedEvent(log: RawActionLog): FeedEvent {
  const blockNumber = log.blockNumber ?? BigInt(0);
  const logIndex = log.logIndex ?? 0;
  const kind = (log.args.kind ?? 0) as ActionKind;
  const payload = (log.args.payload ?? "0x") as Hex;
  return {
    id: `${blockNumber.toString()}:${logIndex}`,
    agentId: Number(log.args.agentId ?? BigInt(0)),
    kind,
    payload,
    timestamp: log.args.timestamp ?? BigInt(0),
    txHash: (log.transactionHash ?? "0x") as Hex,
    blockNumber,
    logIndex,
    trace: decodeActionTrace(kind, payload),
  };
}

/** Per-agent action history for the profile route (backfill, no live cursor). */
export function useAgentActions(agentId: number | undefined) {
  return useQuery<FeedEvent[]>({
    queryKey: ["agent-actions", AGENT_REGISTRY_ADDRESS, agentId],
    enabled: REGISTRY_CONFIGURED && agentId !== undefined && agentId >= 1,
    queryFn: async () => {
      const logs = (await getContractEventsChunked({
        address: REGISTRY,
        abi: agentRegistryAbi,
        eventName: "AgentAction",
        args: { agentId: BigInt(agentId as number) },
        lookback: BigInt(100_000),
      })) as RawActionLog[];
      return logs
        .map(toFeedEvent)
        .sort((a, b) => Number(b.blockNumber - a.blockNumber) || b.logIndex - a.logIndex);
    },
    refetchInterval: 20_000,
  });
}

/* ------------------------------ leaderboard ----------------------------- */

interface RawResolveLog {
  args: { agent?: Address; slashed?: boolean };
}

/**
 * Reputation map: scans AdviceResolved logs from PerformanceOracle and tallies
 * win (!slashed) / loss (slashed) per operator address. Honest empty map when
 * the oracle isn't configured or has no resolves yet.
 */
export function useReputation() {
  return useQuery<Map<string, ReputationRecord>>({
    queryKey: ["arena-reputation", PERFORMANCE_ORACLE_ADDRESS],
    enabled: ORACLE_CONFIGURED,
    queryFn: async () => {
      const logs = (await getContractEventsChunked({
        address: ORACLE,
        abi: performanceOracleAbi,
        eventName: "AdviceResolved",
        lookback: BigInt(100_000),
      })) as RawResolveLog[];

      const map = new Map<string, ReputationRecord>();
      for (const log of logs) {
        const agent = log.args.agent;
        if (!agent) continue;
        const key = agent.toLowerCase();
        const rec = map.get(key) ?? { operator: agent, wins: 0, losses: 0 };
        if (log.args.slashed) rec.losses += 1;
        else rec.wins += 1;
        map.set(key, rec);
      }
      return map;
    },
    refetchInterval: 20_000,
  });
}

/** Look up an operator's win/loss from the reputation map (case-insensitive). */
export function reputationFor(
  map: Map<string, ReputationRecord> | undefined,
  operator: Address,
): ReputationRecord {
  return map?.get(operator.toLowerCase()) ?? { operator, wins: 0, losses: 0 };
}

/* --------------------------- earned visibility -------------------------- */

/**
 * Per-agent proof of real participation. "Earned visibility": registration is
 * open (the contract gates on identity + bond), but the LIVE economy view only
 * surfaces agents that have actually *done work*. Every field is read on-chain:
 *  - bondBalance: current BondVault.balanceOf(operator) (slashing can zero it).
 *  - actionCount: number of AgentAction events the agent has emitted.
 *  - proven: active AND bond > 0 AND >= 1 action. A registered-but-idle ghost
 *    is NOT proven, so it never inflates the proof surface (no fake agents).
 */
export interface AgentProof {
  agentId: number;
  actionCount: number;
  bondBalance: bigint;
  proven: boolean;
}

export function useArenaProof(agents: ArenaAgent[] | undefined) {
  const ids = (agents ?? []).map((a) => a.agentId).join(",");
  return useQuery<Map<number, AgentProof>>({
    queryKey: ["arena-proof", AGENT_REGISTRY_ADDRESS, ids],
    enabled: REGISTRY_CONFIGURED && !!agents && agents.length > 0,
    queryFn: async () => {
      // 1. Tally AgentAction events per agentId, scanning history in <=10k
      //    chunks (the public RPC's getLogs cap).
      const counts = new Map<number, number>();
      const logs = (await getContractEventsChunked({
        address: REGISTRY,
        abi: agentRegistryAbi,
        eventName: "AgentAction",
        lookback: BigInt(100_000),
      })) as { args: { agentId?: bigint } }[];
      for (const l of logs) {
        const id = Number(l.args.agentId ?? BigInt(0));
        counts.set(id, (counts.get(id) ?? 0) + 1);
      }

      // 2. Current bond per agent (balanceOf the operator on its bond vault).
      const map = new Map<number, AgentProof>();
      await Promise.all(
        (agents ?? []).map(async (a) => {
          let bond = BigInt(0);
          try {
            bond = (await publicClient.readContract({
              address: a.bondVault,
              abi: bondVaultAbi,
              functionName: "balanceOf",
              args: [a.operator],
            })) as bigint;
          } catch {
            // Non-vault address or read failure => treat as zero bond.
          }
          const actionCount = counts.get(a.agentId) ?? 0;
          map.set(a.agentId, {
            agentId: a.agentId,
            actionCount,
            bondBalance: bond,
            proven: a.active && bond > BigInt(0) && actionCount >= 1,
          });
        }),
      );
      return map;
    },
    refetchInterval: 20_000,
  });
}

/** Proof for one agent, defaulting to an honest "unproven" record. */
export function proofFor(
  map: Map<number, AgentProof> | undefined,
  agentId: number,
): AgentProof {
  return (
    map?.get(agentId) ?? { agentId, actionCount: 0, bondBalance: BigInt(0), proven: false }
  );
}
