/**
 * spawn_agent.ts — Slice 3 entry point.
 *
 * Pipeline (per docs/superpowers/specs/2026-05-21-constrained-cognition-design.md §5):
 *   1. Generate a Turnkey EOA (TEE-backed) for the agent. This is the x402 signer.
 *   2. Wrap that EOA in a Circle Developer-Controlled SCA on ARC-TESTNET.
 *   3. Install the ConstitutionHook validator module on the SCA, parameterized
 *      with the agent's constitutionHash.
 *   4. Mint an ERC-8004 identity NFT for the SCA via Arc's deployed registry.
 *   5. Issue an ERC-7715-style session key authorizing the Turnkey EOA to act
 *      for the SCA within bounded budget / expiry / scope. If a parent session
 *      is supplied, enforce subset-of-parent locally before submitting.
 *
 * CLI:
 *   tsx scripts/wallet/spawn_agent.ts \
 *     --name bob \
 *     --budget 10 \
 *     --expiry-min 5 \
 *     --constitution-hash 0xabc... \
 *     [--metadata-uri ipfs://...] \
 *     [--dry-run=false]      # default: true
 *
 * stdout: a single line of JSON matching AgentSpawnResult.
 */

import { keccak256, toHex, type Hex } from "viem";
import {
  type AgentSpawnResult,
  type Bytes32,
  type SessionKeyAuth,
  type SessionScope,
  type SpawnAgentInput,
} from "./types.js";
import { createTurnkeyEoa } from "./turnkey_client.js";
import {
  installConstitutionHook,
  wrapEoaInCircleSca,
} from "./circle_sca.js";
import {
  DEFAULT_METADATA_URI,
  mintIdentity,
} from "./erc8004_mint.js";
import { issueSessionKey } from "./erc7715_session.js";

/** Default scopes — the demo's three primary action categories. */
const DEFAULT_SCOPES: SessionScope[] = [
  "x402_pay",
  "trade_execute",
  "memory_anchor",
];

/**
 * Spawn an agent. Returns the full handle the orchestrator needs to drive
 * the agent (SCA address, identity id, session key, EOA, and tx hashes).
 *
 * Idempotency note: each call mints a fresh Turnkey EOA, fresh SCA, and fresh
 * ERC-8004 identity. There is no de-duplication by `name` — that's intentional
 * for the demo (spawning Bob's child agent should not collide with Bob).
 */
export async function spawnAgent(
  input: SpawnAgentInput,
): Promise<AgentSpawnResult> {
  const {
    name,
    budget_USDC,
    expiryMinutes,
    constitutionHash,
    parentSessionKey,
    metadataURI = DEFAULT_METADATA_URI,
    scopes = DEFAULT_SCOPES,
    darkPoolEndpoint,
  } = input;
  const dryRun = input.dryRun ?? true;

  if (!/^0x[0-9a-fA-F]{64}$/.test(constitutionHash)) {
    throw new Error(
      `constitutionHash must be a 32-byte hex string, got ${constitutionHash}`,
    );
  }
  if (budget_USDC < 0) {
    throw new Error(`budget_USDC must be non-negative, got ${budget_USDC}`);
  }
  if (expiryMinutes <= 0) {
    throw new Error(`expiryMinutes must be > 0, got ${expiryMinutes}`);
  }

  // Step 1: Turnkey EOA. In dry-run we force the local random generator so we
  // never accidentally hit the Turnkey API during a test.
  const eoa = await createTurnkeyEoa(name, { forceLocal: dryRun });

  // Step 2: Circle SCA wrapping.
  const sca = await wrapEoaInCircleSca({ owner: eoa, name, dryRun });

  // Step 3: Install the ConstitutionHook on the SCA. This is the load-bearing
  // step that wires Slice 2's contracts into this agent's tx path.
  const install = await installConstitutionHook({
    sca,
    constitutionHash,
    dryRun,
  });

  // Step 4: ERC-8004 identity mint via canonical register(string,(string,bytes)[])
  //         + setAgentWallet binding of the Turnkey EOA to the agentId.
  const identity = await mintIdentity({
    sca,
    constitutionHash,
    metadataURI,
    dryRun,
    turnkeyEoa: eoa,
    agentName: name,
    ...(darkPoolEndpoint !== undefined && { darkPoolEndpoint }),
  });

  // Step 5: Issue the session key. If parent supplied, enforce subset bounds.
  const sessionKey: SessionKeyAuth = await issueSessionKey({
    signer: eoa,
    sca,
    budgetUSDC: budget_USDC,
    expiryMinutes,
    scopes,
    constitutionHash,
    ...(parentSessionKey && { parent: parentSessionKey }),
    dryRun,
  });

  return {
    scaAddress: sca.address,
    identityId: identity.identityId,
    sessionKey,
    turnkeyEoa: eoa.address,
    metadataEntries: identity.metadataEntries,
    txHashes: {
      ...(identity.registerTxHash && { identityMint: identity.registerTxHash }),
      ...(identity.setWalletTxHash && {
        identitySetAgentWallet: identity.setWalletTxHash,
      }),
      ...(sca.deployTxHash && { scaDeploy: sca.deployTxHash }),
      ...(sessionKey.installTxHash && {
        sessionKeyIssue: sessionKey.installTxHash,
      }),
      ...(install.installTxHash && {
        // ConstitutionHook install is a separate event from sessionKey install,
        // but they share the SCA. We surface the hook-install tx under a
        // dedicated key when present (covered by sessionKeyIssue in the
        // current minimal interface; full plumbing arrives with Slice 2).
      }),
    },
  };
}

/**
 * Stable hex hash helper for deriving demo constitution hashes from a rule
 * label, used by demo scripts and tests. Slice 2's ConstitutionRegistry will
 * be the authoritative source of these values in production.
 */
export function demoConstitutionHash(label: string): Bytes32 {
  return keccak256(toHex(`agorahack-demo-constitution|${label}`));
}

/* ------------------------------------------------------------------ */
/*                              CLI shim                              */
/* ------------------------------------------------------------------ */

/** Minimal argv parser. Avoids pulling in commander/yargs for one entry point. */
function parseArgs(argv: string[]): Record<string, string | boolean> {
  const out: Record<string, string | boolean> = {};
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i]!;
    if (!a.startsWith("--")) continue;
    const eq = a.indexOf("=");
    if (eq !== -1) {
      const key = a.slice(2, eq);
      const val = a.slice(eq + 1);
      out[key] = val;
      continue;
    }
    const key = a.slice(2);
    const next = argv[i + 1];
    if (next !== undefined && !next.startsWith("--")) {
      out[key] = next;
      i++;
    } else {
      out[key] = true;
    }
  }
  return out;
}

function asString(v: string | boolean | undefined, name: string): string {
  if (typeof v !== "string") {
    throw new Error(`CLI flag --${name} requires a value`);
  }
  return v;
}

function asBool(v: string | boolean | undefined, fallback: boolean): boolean {
  if (v === undefined) return fallback;
  if (typeof v === "boolean") return v;
  const lower = v.toLowerCase();
  if (lower === "true" || lower === "1" || lower === "yes") return true;
  if (lower === "false" || lower === "0" || lower === "no") return false;
  throw new Error(`Boolean flag expected, got ${v}`);
}

async function main(): Promise<void> {
  const argv = process.argv.slice(2);
  // Support both `pnpm run spawn-agent -- --name bob` (npm strips the `--`) and
  // direct `tsx spawn_agent.ts --name bob`.
  const args = parseArgs(argv);

  const name = asString(args.name, "name");
  const budgetStr = asString(args.budget, "budget");
  const expiryStr = asString(args["expiry-min"], "expiry-min");
  const constitutionHash = asString(
    args["constitution-hash"],
    "constitution-hash",
  ) as Bytes32;
  const dryRun = asBool(args["dry-run"], true);
  const metadataURI =
    typeof args["metadata-uri"] === "string"
      ? (args["metadata-uri"] as string)
      : DEFAULT_METADATA_URI;

  const budget = Number(budgetStr);
  const expiryMinutes = Number(expiryStr);
  if (!Number.isFinite(budget) || !Number.isFinite(expiryMinutes)) {
    throw new Error("budget and expiry-min must be numbers");
  }

  const result = await spawnAgent({
    name,
    budget_USDC: budget,
    expiryMinutes,
    constitutionHash,
    metadataURI,
    dryRun,
  });

  // Print machine-readable JSON to stdout. Human commentary goes to stderr.
  process.stderr.write(
    `[spawn-agent] name=${name} dryRun=${dryRun} sca=${result.scaAddress} ` +
      `identityId=${result.identityId} eoa=${result.turnkeyEoa}\n`,
  );
  process.stdout.write(
    JSON.stringify(result, (_k, v) => (typeof v === "bigint" ? v.toString() : v)) +
      "\n",
  );
}

// Allow `tsx spawn_agent.ts` direct invocation, but also `import { spawnAgent }`.
const isDirectExec =
  typeof process !== "undefined" &&
  process.argv[1] !== undefined &&
  /spawn_agent\.ts$/.test(process.argv[1]);

if (isDirectExec) {
  main().catch((err: unknown) => {
    const msg = err instanceof Error ? err.message : String(err);
    process.stderr.write(`[spawn-agent ERROR] ${msg}\n`);
    process.exit(1);
  });
}

/* Re-export everything the orchestrator + tests need. */
export type {
  AgentSpawnResult,
  SessionKeyAuth,
  SpawnAgentInput,
} from "./types.js";
// `Hex` is exported as a type-only convenience for downstream code.
export type { Hex };
