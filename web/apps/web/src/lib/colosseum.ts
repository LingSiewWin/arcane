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
  isConfiguredAddress,
  type ChaosItemKind,
} from "@/lib/constants";

export const COLOSSEUM_CONFIGURED = isConfiguredAddress(COLOSSEUM_ADDRESS);
export const COLOSSEUM = COLOSSEUM_ADDRESS as Address;

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
