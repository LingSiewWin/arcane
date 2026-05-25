/** On-chain constants for the live viem reads. */

import { env } from "@web/env/web";

/** Pyth oracle contract on Arc testnet. */
export const PYTH_ADDRESS = "0x2880aB155794e7179c9eE2e38200202908C17B43" as const;

/** Pyth SOL/USD price feed id. */
export const SOL_USD_FEED_ID =
  "0xef0d8b6fda2ceba41da15d4095d1da392a0d2f8ed0c6c7bc0f4cfac8c280b56d" as const;

/** Canonical ERC-8004 IdentityRegistry on Arc testnet. */
export const ERC8004_IDENTITY_REGISTRY =
  "0x8004A818BFB912233c491871b3d84c89A494BD9e" as const;

/** Arc native USDC (the settlement asset for bets + chaos fees), 6 decimals. */
export const ARC_USDC_ADDRESS =
  "0x3600000000000000000000000000000000000000" as const;

/* ------------------------------ arena config ----------------------------- */

/**
 * AgentRegistry address. NOT hardcoded — the demo redeploys it every run, so it
 * is read from `NEXT_PUBLIC_AGENT_REGISTRY` at build time. When unset, the arena
 * renders an honest "registry not configured" state instead of fabricating data.
 *
 * Set it in `web/apps/web/.env.local`:
 *   NEXT_PUBLIC_AGENT_REGISTRY=0x...
 */
export const AGENT_REGISTRY_ADDRESS = (env.NEXT_PUBLIC_AGENT_REGISTRY ?? "").trim();

/**
 * Optional PerformanceOracle address for the leaderboard's win/loss derivation.
 * When unset the leaderboard shows an honest "no resolutions yet" state.
 */
export const PERFORMANCE_ORACLE_ADDRESS = (
  env.NEXT_PUBLIC_PERFORMANCE_ORACLE ?? ""
).trim();

/**
 * Colosseum address — the live-duel backbone. Read from env (redeployed per run).
 * Unset → the /colosseum page renders an honest "not configured" state.
 */
export const COLOSSEUM_ADDRESS = (env.NEXT_PUBLIC_COLOSSEUM ?? "").trim();

/**
 * MemoryAnchor address — where agents anchor their RaBitQ memory roots on-chain.
 * Read from env (redeployed per run). Unset → the memory panel shows the live
 * compression from on-chain reasoning events without the "anchored" proof.
 */
export const MEMORY_ANCHOR_ADDRESS = (env.NEXT_PUBLIC_MEMORY_ANCHOR ?? "").trim();

/** True when the env names a plausible 20-byte address. */
export function isConfiguredAddress(value: string): value is `0x${string}` {
  return /^0x[0-9a-fA-F]{40}$/.test(value);
}

/** Chaos item kinds — MUST mirror Colosseum.sol (ITEM_*). */
export const CHAOS_ITEMS = {
  0: {
    name: "Flashbang",
    blurb: "Inject a fabricated news headline into the target's context.",
    tone: "alarm",
  },
  1: {
    name: "Memory Wipe",
    blurb: "Clear the target's short-term memory — strip its recent plan.",
    tone: "signal",
  },
  2: {
    name: "Liquidity Shield",
    blurb: "Defensive buff: lower an agent's liquidation price for a window.",
    tone: "ok",
  },
} as const satisfies Record<number, { name: string; blurb: string; tone: "ok" | "signal" | "alarm" }>;

export type ChaosItemKind = keyof typeof CHAOS_ITEMS;

/** AgentAction.kind enum → human label + tone. Mirrors AgentRegistry.sol. */
export const ACTION_KINDS = {
  0: { label: "advice published", tone: "ok" },
  1: { label: "query paid", tone: "signal" },
  2: { label: "constitution revert", tone: "alarm" },
  3: { label: "bond slashed", tone: "alarm" },
  4: { label: "bond released", tone: "ok" },
} as const satisfies Record<number, { label: string; tone: "ok" | "signal" | "alarm" }>;

export type ActionKind = keyof typeof ACTION_KINDS;
