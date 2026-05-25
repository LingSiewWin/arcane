"use client";

/**
 * Colosseum data layer — reads the live duel, betting pools, chaos-injection
 * ledger, and adversarial-resilience scores straight off the public Arc RPC.
 * No keys, no mocks; honest empty states when no duel/contract is configured.
 */

import { useQuery } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import type { Address, Hex } from "viem";

import { publicClient } from "@/lib/chain";
import {
  ARC_USDC_ADDRESS,
  COLOSSEUM_ADDRESS,
  MEMORY_ANCHOR_ADDRESS,
  isConfiguredAddress,
  type ChaosItemKind,
} from "@/lib/constants";

export const COLOSSEUM_CONFIGURED = isConfiguredAddress(COLOSSEUM_ADDRESS);
export const COLOSSEUM = COLOSSEUM_ADDRESS as Address;
export const MEMORY_ANCHOR_CONFIGURED = isConfiguredAddress(MEMORY_ANCHOR_ADDRESS);
export const MEMORY_ANCHOR = MEMORY_ANCHOR_ADDRESS as Address;

/** MemoryAnchor — the on-chain commitment of an agent's compressed memory root.
 *  `agent` is indexed, so the memory panel can filter anchors per agent. */
export const memoryAnchorAbi = [
  {
    type: "event",
    name: "MemoryAnchored",
    inputs: [
      { name: "agent", type: "address", indexed: true },
      { name: "identityId", type: "uint256", indexed: true },
      { name: "root", type: "bytes32", indexed: false },
      { name: "timestamp", type: "uint256", indexed: false },
    ],
    anonymous: false,
  },
] as const;

export const colosseumAbi = [
  { type: "function", name: "duelCount", stateMutability: "view", inputs: [], outputs: [{ type: "uint256" }] },
  {
    type: "function",
    name: "getDuel",
    stateMutability: "view",
    inputs: [{ name: "duelId", type: "uint256" }],
    outputs: [
      {
        type: "tuple",
        components: [
          { name: "agentA", type: "address" },
          { name: "agentB", type: "address" },
          { name: "startAt", type: "uint64" },
          { name: "tradingStartsAt", type: "uint64" },
          { name: "endsAt", type: "uint64" },
          { name: "status", type: "uint8" },
          { name: "winner", type: "address" },
          { name: "shieldWinner", type: "address" },
          { name: "scoreA", type: "int256" },
          { name: "scoreB", type: "int256" },
          { name: "poolA", type: "uint256" },
          { name: "poolB", type: "uint256" },
        ],
      },
    ],
  },
  {
    type: "function",
    name: "resilienceOf",
    stateMutability: "view",
    inputs: [{ name: "agent", type: "address" }],
    outputs: [{ name: "ingested", type: "uint256" }, { name: "survived", type: "uint256" }],
  },
  {
    type: "function",
    name: "itemPrice",
    stateMutability: "view",
    inputs: [{ type: "uint8" }],
    outputs: [{ type: "uint256" }],
  },
  {
    type: "function",
    name: "betOf",
    stateMutability: "view",
    inputs: [{ type: "uint256" }, { type: "bool" }, { type: "address" }],
    outputs: [{ type: "uint256" }],
  },
  {
    type: "function",
    name: "claimed",
    stateMutability: "view",
    inputs: [{ type: "uint256" }, { type: "address" }],
    outputs: [{ type: "bool" }],
  },
  {
    type: "function",
    name: "bet",
    stateMutability: "nonpayable",
    inputs: [{ name: "duelId", type: "uint256" }, { name: "onA", type: "bool" }, { name: "amount", type: "uint256" }],
    outputs: [],
  },
  {
    type: "function",
    name: "injectChaos",
    stateMutability: "nonpayable",
    inputs: [{ name: "duelId", type: "uint256" }, { name: "target", type: "address" }, { name: "itemKind", type: "uint8" }],
    outputs: [],
  },
  {
    type: "function",
    name: "claim",
    stateMutability: "nonpayable",
    inputs: [{ name: "duelId", type: "uint256" }],
    outputs: [{ type: "uint256" }],
  },
  {
    type: "function",
    name: "registerAgent",
    stateMutability: "nonpayable",
    inputs: [{ name: "agent", type: "address" }],
    outputs: [],
  },
  {
    type: "function",
    name: "fundPrizePool",
    stateMutability: "nonpayable",
    inputs: [{ name: "duelId", type: "uint256" }, { name: "amount", type: "uint256" }],
    outputs: [],
  },
  {
    type: "function",
    name: "agents",
    stateMutability: "view",
    inputs: [{ type: "address" }],
    outputs: [
      { name: "developer", type: "address" },
      { name: "stake", type: "uint256" },
      { name: "failures", type: "uint256" },
      { name: "registered", type: "bool" },
    ],
  },
  {
    type: "function",
    name: "prizePool",
    stateMutability: "view",
    inputs: [{ type: "uint256" }],
    outputs: [{ type: "uint256" }],
  },
  {
    type: "function",
    name: "stakeRequirement",
    stateMutability: "view",
    inputs: [],
    outputs: [{ type: "uint256" }],
  },
  {
    type: "event",
    name: "ChaosInjected",
    inputs: [
      { name: "injectionId", type: "uint256", indexed: true },
      { name: "duelId", type: "uint256", indexed: true },
      { name: "target", type: "address", indexed: true },
      { name: "spectator", type: "address", indexed: false },
      { name: "itemKind", type: "uint8", indexed: false },
      { name: "fee", type: "uint256", indexed: false },
      { name: "escrow", type: "uint256", indexed: false },
    ],
    anonymous: false,
  },
  {
    type: "event",
    name: "BountyPaid",
    inputs: [
      { name: "injectionId", type: "uint256", indexed: true },
      { name: "duelId", type: "uint256", indexed: true },
      { name: "developer", type: "address", indexed: true },
      { name: "amount", type: "uint256", indexed: false },
    ],
    anonymous: false,
  },
  {
    type: "event",
    name: "DuelResolved",
    inputs: [
      { name: "duelId", type: "uint256", indexed: true },
      { name: "alphaWinner", type: "address", indexed: true },
      { name: "shieldWinner", type: "address", indexed: true },
      { name: "scoreA", type: "int256", indexed: false },
      { name: "scoreB", type: "int256", indexed: false },
      { name: "prizePool", type: "uint256", indexed: false },
    ],
    anonymous: false,
  },
  {
    type: "event",
    name: "CallReported",
    inputs: [
      { name: "duelId", type: "uint256", indexed: true },
      { name: "agent", type: "address", indexed: true },
      { name: "injectionId", type: "uint256", indexed: false },
      { name: "rBps", type: "int256", indexed: false },
      { name: "ingestedInjection", type: "bool", indexed: false },
      { name: "survived", type: "bool", indexed: false },
      { name: "failed", type: "bool", indexed: false },
    ],
    anonymous: false,
  },
  {
    type: "event",
    name: "AgentReasoning",
    inputs: [
      { name: "duelId", type: "uint256", indexed: true },
      { name: "agent", type: "address", indexed: true },
      { name: "cycle", type: "uint16", indexed: false },
      { name: "ingestedInjection", type: "bool", indexed: false },
      { name: "survived", type: "bool", indexed: false },
      { name: "reasoning", type: "string", indexed: false },
    ],
    anonymous: false,
  },
] as const;

export type DuelStatus = 0 | 1 | 2; // None / Live / Resolved

export interface Duel {
  duelId: number;
  agentA: Address;
  agentB: Address;
  startAt: bigint;
  tradingStartsAt: bigint;
  endsAt: bigint;
  status: DuelStatus;
  winner: Address;        // Alpha (PnL) winner — also the parimutuel winner
  shieldWinner: Address;  // Iron Shield (resilience) winner
  scoreA: bigint;
  scoreB: bigint;
  poolA: bigint;
  poolB: bigint;
}

export interface Resilience {
  ingested: number;
  survived: number;
}

export interface InjectionEvent {
  id: string;
  injectionId: bigint;
  duelId: number;
  target: Address;
  spectator: Address;
  itemKind: ChaosItemKind;
  fee: bigint;
  txHash: Hex;
  blockNumber: bigint;
}

/** The latest duel (highest id). Honest null when none / not configured. */
export function useActiveDuel() {
  return useQuery<Duel | null>({
    queryKey: ["colosseum-active-duel", COLOSSEUM_ADDRESS],
    enabled: COLOSSEUM_CONFIGURED,
    queryFn: async () => {
      const count = (await publicClient.readContract({
        address: COLOSSEUM,
        abi: colosseumAbi,
        functionName: "duelCount",
      })) as bigint;
      const n = Number(count);
      if (n === 0) return null;
      const d = (await publicClient.readContract({
        address: COLOSSEUM,
        abi: colosseumAbi,
        functionName: "getDuel",
        args: [BigInt(n)],
      })) as Omit<Duel, "duelId">;
      return { duelId: n, ...d, status: d.status as DuelStatus };
    },
    refetchInterval: 4_000,
  });
}

export function useResilience(agent: Address | undefined) {
  return useQuery<Resilience>({
    queryKey: ["colosseum-resilience", COLOSSEUM_ADDRESS, agent],
    enabled: COLOSSEUM_CONFIGURED && !!agent,
    queryFn: async () => {
      const [ingested, survived] = (await publicClient.readContract({
        address: COLOSSEUM,
        abi: colosseumAbi,
        functionName: "resilienceOf",
        args: [agent as Address],
      })) as [bigint, bigint];
      return { ingested: Number(ingested), survived: Number(survived) };
    },
    refetchInterval: 6_000,
  });
}

interface RawInjLog {
  args: { injectionId?: bigint; duelId?: bigint; target?: Address; spectator?: Address; itemKind?: number; fee?: bigint };
  transactionHash: Hex | null;
  blockNumber: bigint | null;
  logIndex: number | null;
}

/** Streams ChaosInjected events (backfill + 4s poll) → toasts + Top Saboteurs. */
export function useInjections(duelId: number | undefined, cap = 50) {
  const [events, setEvents] = useState<InjectionEvent[]>([]);
  const seen = useRef<Set<string>>(new Set());

  useEffect(() => {
    if (!COLOSSEUM_CONFIGURED || !duelId) return;
    let cancelled = false;
    const toEvent = (l: RawInjLog): InjectionEvent => ({
      id: `${(l.blockNumber ?? BigInt(0)).toString()}:${l.logIndex ?? 0}`,
      injectionId: l.args.injectionId ?? BigInt(0),
      duelId: Number(l.args.duelId ?? BigInt(0)),
      target: (l.args.target ?? "0x") as Address,
      spectator: (l.args.spectator ?? "0x") as Address,
      itemKind: ((l.args.itemKind ?? 0) as ChaosItemKind),
      fee: l.args.fee ?? BigInt(0),
      txHash: (l.transactionHash ?? "0x") as Hex,
      blockNumber: l.blockNumber ?? BigInt(0),
    });
    const ingest = (incoming: InjectionEvent[]) => {
      if (cancelled) return;
      const fresh = incoming.filter((e) => !seen.current.has(e.id));
      if (fresh.length === 0) return;
      for (const e of fresh) seen.current.add(e.id);
      fresh.sort((a, b) => Number(b.blockNumber - a.blockNumber));
      setEvents((prev) => [...fresh, ...prev].slice(0, cap));
    };
    (async () => {
      try {
        const head = await publicClient.getBlockNumber();
        const window = BigInt(9_000);
        const fromBlock = head > window ? head - window : BigInt(0);
        const logs = (await publicClient.getContractEvents({
          address: COLOSSEUM,
          abi: colosseumAbi,
          eventName: "ChaosInjected",
          args: { duelId: BigInt(duelId) },
          fromBlock,
          toBlock: "latest",
        })) as unknown as RawInjLog[];
        ingest(logs.map(toEvent));
      } catch {
        /* poll below still streams */
      }
    })();
    const unwatch = publicClient.watchContractEvent({
      address: COLOSSEUM,
      abi: colosseumAbi,
      eventName: "ChaosInjected",
      args: { duelId: BigInt(duelId) },
      poll: true,
      pollingInterval: 4_000,
      onLogs: (logs) => ingest((logs as unknown as RawInjLog[]).map(toEvent)),
    });
    return () => {
      cancelled = true;
      unwatch();
    };
  }, [duelId, cap]);

  return events;
}

export interface ReasoningEvent {
  id: string;
  agent: Address;
  cycle: number;
  ingested: boolean;
  survived: boolean;
  reasoning: string;
  blockNumber: bigint;
}

interface RawReasoningLog {
  args: { agent?: Address; cycle?: number; ingestedInjection?: boolean; survived?: boolean; reasoning?: string };
  blockNumber: bigint | null;
  logIndex: number | null;
}

/** Streams AgentReasoning events for a duel → the live chain-of-thought feed. */
export function useReasoning(duelId: number | undefined, cap = 60) {
  const [events, setEvents] = useState<ReasoningEvent[]>([]);
  const seen = useRef<Set<string>>(new Set());

  useEffect(() => {
    if (!COLOSSEUM_CONFIGURED || !duelId) return;
    let cancelled = false;
    const toEvent = (l: RawReasoningLog): ReasoningEvent => ({
      id: `${(l.blockNumber ?? BigInt(0)).toString()}:${l.logIndex ?? 0}`,
      agent: (l.args.agent ?? "0x") as Address,
      cycle: Number(l.args.cycle ?? 0),
      ingested: Boolean(l.args.ingestedInjection),
      survived: Boolean(l.args.survived),
      reasoning: l.args.reasoning ?? "",
      blockNumber: l.blockNumber ?? BigInt(0),
    });
    const ingest = (incoming: ReasoningEvent[]) => {
      if (cancelled) return;
      const fresh = incoming.filter((e) => !seen.current.has(e.id));
      if (fresh.length === 0) return;
      for (const e of fresh) seen.current.add(e.id);
      fresh.sort((a, b) => Number(b.blockNumber - a.blockNumber));
      setEvents((prev) => [...fresh, ...prev].slice(0, cap));
    };
    (async () => {
      try {
        const head = await publicClient.getBlockNumber();
        const window = BigInt(9_000);
        const fromBlock = head > window ? head - window : BigInt(0);
        const logs = (await publicClient.getContractEvents({
          address: COLOSSEUM,
          abi: colosseumAbi,
          eventName: "AgentReasoning",
          args: { duelId: BigInt(duelId) },
          fromBlock,
          toBlock: "latest",
        })) as unknown as RawReasoningLog[];
        ingest(logs.map(toEvent));
      } catch {
        /* poll below still streams */
      }
    })();
    const unwatch = publicClient.watchContractEvent({
      address: COLOSSEUM,
      abi: colosseumAbi,
      eventName: "AgentReasoning",
      args: { duelId: BigInt(duelId) },
      poll: true,
      pollingInterval: 4_000,
      onLogs: (logs) => ingest((logs as unknown as RawReasoningLog[]).map(toEvent)),
    });
    return () => {
      cancelled = true;
      unwatch();
    };
  }, [duelId, cap]);

  return events;
}

/* ------------------------------ agent memory ----------------------------- */

/**
 * 1-bit RaBitQ memory accounting — the MEASURED constants from the bench,
 * deterministic from the 384-d MiniLM-L6-v2 embedder. A vector compresses to a
 * 56-byte code (48 B packed sign bits + a 4 B L1 scalar + a 4 B norm) vs 1536 B
 * for FP32 → 27.4×. These are not marketing numbers; they're the exact layout
 * the agent stores, so `entries * BYTES_PER_VEC` is the agent's real on-chain
 * memory footprint.
 */
export const MEMORY_BYTES_PER_VEC = 56;
export const MEMORY_FP32_BYTES_PER_VEC = 1536;
export const MEMORY_COMPRESSION_X = 27.4;
export const MEMORY_EMBED_DIM = 384;

export interface AgentMemory {
  /** Count of AgentReasoning traces this agent has stored on-chain. */
  entries: number;
  /** Bytes the compressed (1-bit RaBitQ) memory occupies: entries * 56. */
  bytes: number;
  /** What an FP32 store of the same traces would cost: entries * 1536. */
  fp32: number;
  /** Measured compression ratio (FP32 / RaBitQ). */
  compressionX: number;
  /** The latest on-chain anchored memory root, or null if never anchored. */
  anchoredRoot: Hex | null;
  /** The identity the latest root was anchored under, or null. */
  identityId: bigint | null;
}

interface RawAnchorLog {
  args: { agent?: Address; identityId?: bigint; root?: Hex; timestamp?: bigint };
  blockNumber: bigint | null;
  logIndex: number | null;
}

/**
 * Live per-agent memory: counts the agent's AgentReasoning traces (the REAL
 * reasoning it has stored on-chain) and reads its latest anchored RaBitQ root
 * as proof. The byte figures are derived from that count via the measured
 * 56 B/vector RaBitQ layout — so the panel answers "what are you compressing?"
 * with the agent's own data, not a static corpus.
 *
 * Both reads use the 9000-block window the public Arc RPC tolerates for
 * getLogs (the same window useReasoning/useInjections use), filtered by the
 * indexed `agent` topic so the scan returns only this agent's events.
 */
export function useAgentMemory(agent: Address | undefined) {
  return useQuery<AgentMemory>({
    queryKey: ["colosseum-agent-memory", COLOSSEUM_ADDRESS, MEMORY_ANCHOR_ADDRESS, agent],
    enabled: COLOSSEUM_CONFIGURED && !!agent,
    queryFn: async () => {
      const head = await publicClient.getBlockNumber();
      const window = BigInt(9_000);
      const fromBlock = head > window ? head - window : BigInt(0);

      // 1. Count this agent's stored reasoning traces (filter by indexed agent).
      const reasoningLogs = (await publicClient.getContractEvents({
        address: COLOSSEUM,
        abi: colosseumAbi,
        eventName: "AgentReasoning",
        args: { agent: agent as Address },
        fromBlock,
        toBlock: "latest",
      })) as unknown as RawReasoningLog[];
      const entries = reasoningLogs.length;

      // 2. Latest MemoryAnchored root for this agent (highest block), if the
      //    anchor contract is configured. Filtered by the indexed agent topic.
      let anchoredRoot: Hex | null = null;
      let identityId: bigint | null = null;
      if (MEMORY_ANCHOR_CONFIGURED) {
        try {
          const anchorLogs = (await publicClient.getContractEvents({
            address: MEMORY_ANCHOR,
            abi: memoryAnchorAbi,
            eventName: "MemoryAnchored",
            args: { agent: agent as Address },
            fromBlock,
            toBlock: "latest",
          })) as unknown as RawAnchorLog[];
          let best: RawAnchorLog | null = null;
          for (const l of anchorLogs) {
            const bn = l.blockNumber ?? BigInt(0);
            const bestBn = best?.blockNumber ?? BigInt(-1);
            if (bn > bestBn || (bn === bestBn && (l.logIndex ?? 0) >= (best?.logIndex ?? 0))) {
              best = l;
            }
          }
          if (best?.args.root) {
            anchoredRoot = best.args.root;
            identityId = best.args.identityId ?? null;
          }
        } catch {
          // Anchor scan failed → fall through with no proof (entries still show).
        }
      }

      return {
        entries,
        bytes: entries * MEMORY_BYTES_PER_VEC,
        fp32: entries * MEMORY_FP32_BYTES_PER_VEC,
        compressionX: MEMORY_COMPRESSION_X,
        anchoredRoot,
        identityId,
      };
    },
    refetchInterval: 6_000,
  });
}

/* ---------------------------- arena standings --------------------------- */

/**
 * One agent's standing in the arena, derived ENTIRELY from on-chain data:
 *  - alphaBps: cumulative PnL — the sum of every CallReported `rBps` the agent
 *    has emitted (a scored trading call). The basis for the Alpha ranking.
 *  - ingested / survived: read live from resilienceOf(agent) — how many chaos
 *    injections it ate vs. how many it survived.
 *  - resilience: survived / ingested in [0,1] (0 when nothing was ingested) —
 *    the basis for the Iron Shield ranking (manipulation resilience).
 */
export interface ArenaStanding {
  address: Address;
  alphaBps: number;
  ingested: number;
  survived: number;
  resilience: number;
}

interface RawCallReportedLog {
  args: { agent?: Address; rBps?: bigint };
}

/**
 * Arena standings: the cross-duel scoreboard. Reads ALL CallReported events
 * (windowed in <=9k-block chunks to respect the public RPC getLogs cap), groups
 * by `agent` and sums `rBps` into a cumulative alphaBps. The set of agents IS
 * the set that appears in CallReported — only agents with a scored call rank.
 * For each such agent it then reads resilienceOf(agent) for the Iron Shield
 * dimension. Honest empty array when not configured / no scored calls yet.
 */
export function useArenaStandings() {
  return useQuery<ArenaStanding[]>({
    queryKey: ["colosseum-arena-standings", COLOSSEUM_ADDRESS],
    enabled: COLOSSEUM_CONFIGURED,
    queryFn: async () => {
      // 1. Scan CallReported history in <=9k-block windows (public RPC cap),
      //    fetched concurrently, then sum rBps per agent.
      const head = await publicClient.getBlockNumber();
      const lookback = BigInt(100_000);
      const maxRange = BigInt(9_000);
      const start = head > lookback ? head - lookback : BigInt(0);

      const ranges: Array<[bigint, bigint]> = [];
      let from = start;
      while (from <= head) {
        const to = from + maxRange < head ? from + maxRange : head;
        ranges.push([from, to]);
        if (to >= head) break;
        from = to + BigInt(1);
      }

      const batches = await Promise.all(
        ranges.map(([f, t]) =>
          publicClient
            .getContractEvents({
              address: COLOSSEUM,
              abi: colosseumAbi,
              eventName: "CallReported",
              fromBlock: f,
              toBlock: t,
            })
            .catch(() => [] as unknown[]),
        ),
      );
      const logs = batches.flat() as unknown as RawCallReportedLog[];

      const alpha = new Map<string, { address: Address; alphaBps: number }>();
      for (const l of logs) {
        const agent = l.args.agent;
        if (!agent) continue;
        const key = agent.toLowerCase();
        const rec = alpha.get(key) ?? { address: agent, alphaBps: 0 };
        rec.alphaBps += Number(l.args.rBps ?? BigInt(0));
        alpha.set(key, rec);
      }

      // 2. For each agent that has reported a call, read its live resilience.
      const entries = [...alpha.values()];
      return Promise.all(
        entries.map(async (e) => {
          const [ingested, survived] = (await publicClient.readContract({
            address: COLOSSEUM,
            abi: colosseumAbi,
            functionName: "resilienceOf",
            args: [e.address],
          })) as [bigint, bigint];
          const ing = Number(ingested);
          const surv = Number(survived);
          return {
            address: e.address,
            alphaBps: e.alphaBps,
            ingested: ing,
            survived: surv,
            resilience: ing === 0 ? 0 : surv / ing,
          };
        }),
      );
    },
    refetchInterval: 6_000,
  });
}

/** Parimutuel implied probability for side A from the pools (0..1). */
export function impliedProbA(poolA: bigint, poolB: bigint): number {
  const total = poolA + poolB;
  if (total === BigInt(0)) return 0.5;
  return Number((poolA * BigInt(10_000)) / total) / 10_000;
}

export type DuelPhase = "betting" | "trading" | "ended" | "resolved";

/** Derive the duel's current phase + a countdown to the next transition. */
export function duelPhase(
  d: Duel,
  nowSec: number,
): { phase: DuelPhase; label: string; secondsToNext: number } {
  if (d.status === 2) return { phase: "resolved", label: "resolved", secondsToNext: 0 };
  const trading = Number(d.tradingStartsAt);
  const ends = Number(d.endsAt);
  if (nowSec < trading)
    return { phase: "betting", label: "betting · trading in", secondsToNext: trading - nowSec };
  if (nowSec < ends)
    return { phase: "trading", label: "live · ends in", secondsToNext: ends - nowSec };
  return { phase: "ended", label: "awaiting resolve", secondsToNext: 0 };
}

/** mm:ss countdown formatter. */
export function fmtCountdown(seconds: number): string {
  if (seconds <= 0) return "0:00";
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  return `${m}:${s.toString().padStart(2, "0")}`;
}

const CHAOS_KINDS = [0, 1, 2] as const;

/** On-chain chaos-item prices (6-dec USDC) keyed by item kind. A price of 0n
 *  means the item is disabled on-chain. Read from the contract so the UI never
 *  shows a stale/guessed fee (and never lets a spectator submit an injectChaos
 *  that reverts because the real fee differs from a hardcoded one). */
export function useItemPrices() {
  return useQuery<Record<ChaosItemKind, bigint>>({
    queryKey: ["colosseum-item-prices", COLOSSEUM_ADDRESS],
    enabled: COLOSSEUM_CONFIGURED,
    queryFn: async () => {
      const prices = (await Promise.all(
        CHAOS_KINDS.map(
          (k) =>
            publicClient.readContract({
              address: COLOSSEUM,
              abi: colosseumAbi,
              functionName: "itemPrice",
              args: [k],
            }) as Promise<bigint>,
        ),
      )) as bigint[];
      return { 0: prices[0], 1: prices[1], 2: prices[2] } as Record<ChaosItemKind, bigint>;
    },
    staleTime: 60_000,
  });
}

const erc20AllowanceAbi = [
  {
    type: "function",
    name: "allowance",
    stateMutability: "view",
    inputs: [{ type: "address" }, { type: "address" }],
    outputs: [{ type: "uint256" }],
  },
] as const;

/** The connected wallet's USDC allowance to the Colosseum (6-dec). */
export function useAllowance(account: Address | undefined) {
  return useQuery<bigint>({
    queryKey: ["colosseum-allowance", COLOSSEUM_ADDRESS, account],
    enabled: COLOSSEUM_CONFIGURED && !!account,
    queryFn: async () =>
      (await publicClient.readContract({
        address: ARC_USDC_ADDRESS,
        abi: erc20AllowanceAbi,
        functionName: "allowance",
        args: [account as Address, COLOSSEUM],
      })) as bigint,
    refetchInterval: 5_000,
  });
}

export interface ClaimPosition {
  /** True if the winning side was A. */
  winnerIsA: boolean;
  /** No-winner duel → everyone refunds their own stake. */
  refund: boolean;
  /** USDC stake this account can claim/refund (6-dec). */
  myStake: bigint;
  /** Already claimed this duel. */
  claimed: boolean;
  /** There is something to claim and it hasn't been claimed yet. */
  claimable: boolean;
}

/** The connected wallet's claimable position on a RESOLVED duel. Honest null
 *  until the duel resolves. Mirrors Colosseum.claim()'s payout rule: winners
 *  split the pot; if the winning pool is empty, every bettor refunds. */
export function useClaimPosition(
  duel: Duel | null | undefined,
  account: Address | undefined,
) {
  return useQuery<ClaimPosition | null>({
    queryKey: ["colosseum-claim", COLOSSEUM_ADDRESS, duel?.duelId, account],
    enabled: COLOSSEUM_CONFIGURED && !!duel && duel.status === 2 && !!account,
    queryFn: async () => {
      const d = duel as Duel;
      const acct = account as Address;
      const winnerIsA = d.winner.toLowerCase() === d.agentA.toLowerCase();
      const [stakeA, stakeB, claimed] = (await Promise.all([
        publicClient.readContract({
          address: COLOSSEUM,
          abi: colosseumAbi,
          functionName: "betOf",
          args: [BigInt(d.duelId), true, acct],
        }) as Promise<bigint>,
        publicClient.readContract({
          address: COLOSSEUM,
          abi: colosseumAbi,
          functionName: "betOf",
          args: [BigInt(d.duelId), false, acct],
        }) as Promise<bigint>,
        publicClient.readContract({
          address: COLOSSEUM,
          abi: colosseumAbi,
          functionName: "claimed",
          args: [BigInt(d.duelId), acct],
        }) as Promise<boolean>,
      ])) as [bigint, bigint, boolean];

      const winningPool = winnerIsA ? d.poolA : d.poolB;
      const refund = winningPool === BigInt(0);
      const myStake = refund ? stakeA + stakeB : winnerIsA ? stakeA : stakeB;
      return {
        winnerIsA,
        refund,
        myStake,
        claimed,
        claimable: !claimed && myStake > BigInt(0),
      };
    },
    refetchInterval: 6_000,
  });
}

export interface AgentInfo {
  developer: Address;
  stake: bigint;
  failures: number;
  registered: boolean;
}

/** A registered duelist's on-chain stake/developer info. Null until configured. */
export function useAgentInfo(agent: Address | undefined) {
  return useQuery<AgentInfo | null>({
    queryKey: ["colosseum-agent", COLOSSEUM_ADDRESS, agent],
    enabled: COLOSSEUM_CONFIGURED && !!agent,
    queryFn: async () => {
      const [developer, stake, failures, registered] = (await publicClient.readContract({
        address: COLOSSEUM,
        abi: colosseumAbi,
        functionName: "agents",
        args: [agent as Address],
      })) as [Address, bigint, bigint, boolean];
      return { developer, stake, failures: Number(failures), registered };
    },
    refetchInterval: 8_000,
  });
}

/** The duel's developer prize pool (6-dec USDC): fooled escrows + forfeited
 *  stakes + sponsor seed, split 50/50 between Alpha and Iron Shield at resolve. */
export function usePrizePool(duelId: number | undefined) {
  return useQuery<bigint>({
    queryKey: ["colosseum-prizepool", COLOSSEUM_ADDRESS, duelId],
    enabled: COLOSSEUM_CONFIGURED && !!duelId,
    queryFn: async () =>
      (await publicClient.readContract({
        address: COLOSSEUM,
        abi: colosseumAbi,
        functionName: "prizePool",
        args: [BigInt(duelId as number)],
      })) as bigint,
    refetchInterval: 6_000,
  });
}

export interface BountyEvent {
  id: string;
  injectionId: bigint;
  developer: Address;
  amount: bigint;
  blockNumber: bigint;
}

interface RawBountyLog {
  args: { injectionId?: bigint; developer?: Address; amount?: bigint };
  blockNumber: bigint | null;
  logIndex: number | null;
}

/** Streams BountyPaid events for a duel — the "defense paid off" feed: every
 *  injection an agent survived routes its escrow to the developer. */
export function useBounties(duelId: number | undefined, cap = 20) {
  const [events, setEvents] = useState<BountyEvent[]>([]);
  const seen = useRef<Set<string>>(new Set());

  useEffect(() => {
    if (!COLOSSEUM_CONFIGURED || !duelId) return;
    let cancelled = false;
    const toEvent = (l: RawBountyLog): BountyEvent => ({
      id: `${(l.blockNumber ?? BigInt(0)).toString()}:${l.logIndex ?? 0}`,
      injectionId: l.args.injectionId ?? BigInt(0),
      developer: (l.args.developer ?? "0x") as Address,
      amount: l.args.amount ?? BigInt(0),
      blockNumber: l.blockNumber ?? BigInt(0),
    });
    const ingest = (incoming: BountyEvent[]) => {
      if (cancelled) return;
      const fresh = incoming.filter((e) => !seen.current.has(e.id));
      if (fresh.length === 0) return;
      for (const e of fresh) seen.current.add(e.id);
      fresh.sort((a, b) => Number(b.blockNumber - a.blockNumber));
      setEvents((prev) => [...fresh, ...prev].slice(0, cap));
    };
    (async () => {
      try {
        const head = await publicClient.getBlockNumber();
        const window = BigInt(9_000);
        const fromBlock = head > window ? head - window : BigInt(0);
        const logs = (await publicClient.getContractEvents({
          address: COLOSSEUM,
          abi: colosseumAbi,
          eventName: "BountyPaid",
          args: { duelId: BigInt(duelId) },
          fromBlock,
          toBlock: "latest",
        })) as unknown as RawBountyLog[];
        ingest(logs.map(toEvent));
      } catch {
        /* poll below still streams */
      }
    })();
    const unwatch = publicClient.watchContractEvent({
      address: COLOSSEUM,
      abi: colosseumAbi,
      eventName: "BountyPaid",
      args: { duelId: BigInt(duelId) },
      poll: true,
      pollingInterval: 5_000,
      onLogs: (logs) => ingest((logs as unknown as RawBountyLog[]).map(toEvent)),
    });
    return () => {
      cancelled = true;
      unwatch();
    };
  }, [duelId, cap]);

  return events;
}
