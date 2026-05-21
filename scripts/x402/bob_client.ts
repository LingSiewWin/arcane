#!/usr/bin/env tsx
/**
 * bob_client.ts — CLI for Bob's x402 batch client.
 *
 * The next slice (5D's ``demo_e2e.py``) subprocess-invokes this binary, so
 * the CLI surface is the load-bearing contract:
 *
 *   tsx scripts/x402/bob_client.ts query <url>             \
 *       --vec <vec_json>                                    \
 *       --max-usdc 0.001                                    \
 *       --from-spawn-result <path-to-spawn.json>            \
 *       [--k 10]                                            \
 *       [--chain-id 5042002]                                \
 *       [--network arc-testnet]                             \
 *       [--usdc 0x3600000000000000000000000000000000000000] \
 *       [--mode auto|immediate|manual]                      \
 *       [--max-batch-size 100]                              \
 *       [--max-batch-age 30]                                \
 *       [--flush-after]                                     \
 *       [--local-eoa-key 0x...]
 *
 * stdout: a single line of JSON:
 *   {
 *     "ok": true,
 *     "url": "...",
 *     "result": <server's /query response>,
 *     "payment": { ...PaymentAuthorization with bigints as decimal strings... },
 *     "client_state": { ...X402BatchClientState... },
 *     "settlement": <BatchedSettlement | null>
 *   }
 *
 * stderr: human commentary + warnings.
 *
 * Exit code 0 on success, 1 on x402-level failure (server refused, amount
 * exceeded, etc.), 2 on argument errors.
 *
 * The ``--from-spawn-result`` flag points at the JSON file written by
 * ``scripts/wallet/spawn_agent.ts`` (Slice 3). We read it for Bob's
 * Turnkey EOA address and (when the spawn was local-only) the embedded
 * private key. For Turnkey-backed EOAs we currently surface a clean
 * error — the Turnkey signing path lives in Slice 3 and is not wired
 * into this slice's signer.
 */

import { readFileSync } from "node:fs";
import { resolve as resolvePath } from "node:path";
import type { Address, Hex } from "viem";
import { X402BatchClient } from "./batch_client.js";
import {
  type BatchMode,
  type RawEoa,
  X402Error,
} from "./types.js";

/* ------------------------------------------------------------------ */
/*                          Arg parser                                */
/* ------------------------------------------------------------------ */

interface ParsedArgs {
  positional: string[];
  flags: Record<string, string | boolean>;
}

function parseArgs(argv: string[]): ParsedArgs {
  const positional: string[] = [];
  const flags: Record<string, string | boolean> = {};
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i]!;
    if (!a.startsWith("--")) {
      positional.push(a);
      continue;
    }
    const eq = a.indexOf("=");
    if (eq !== -1) {
      flags[a.slice(2, eq)] = a.slice(eq + 1);
      continue;
    }
    const key = a.slice(2);
    const next = argv[i + 1];
    if (next !== undefined && !next.startsWith("--")) {
      flags[key] = next;
      i++;
    } else {
      flags[key] = true;
    }
  }
  return { positional, flags };
}

function requireString(
  flags: Record<string, string | boolean>,
  name: string,
): string {
  const v = flags[name];
  if (typeof v !== "string") {
    throw new CliError(`--${name} requires a string value`);
  }
  return v;
}

function optString(
  flags: Record<string, string | boolean>,
  name: string,
): string | undefined {
  const v = flags[name];
  return typeof v === "string" ? v : undefined;
}

function optBool(
  flags: Record<string, string | boolean>,
  name: string,
  fallback: boolean,
): boolean {
  const v = flags[name];
  if (v === undefined) return fallback;
  if (typeof v === "boolean") return v;
  const lower = v.toLowerCase();
  if (lower === "true" || lower === "1" || lower === "yes") return true;
  if (lower === "false" || lower === "0" || lower === "no") return false;
  throw new CliError(`--${name} expects a boolean, got ${v}`);
}

class CliError extends Error {
  constructor(msg: string) {
    super(msg);
    this.name = "CliError";
  }
}

/* ------------------------------------------------------------------ */
/*                       Spawn-result loader                          */
/* ------------------------------------------------------------------ */

/**
 * Subset of ``AgentSpawnResult`` (scripts/wallet/types.ts) we actually need.
 *
 * For a Turnkey-backed EOA, the file does NOT contain a private key. The
 * caller must instead set ``--local-eoa-key`` (test path) or rely on the
 * Turnkey signing path (not yet wired in this slice). We surface a clear
 * error on the second path.
 */
interface SpawnResultFile {
  turnkeyEoa: string;
  scaAddress: string;
  identityId?: string;
  /** Optional, only present in local-EOA test runs. */
  turnkeyEoaPrivateKey?: string;
}

function loadSpawnResult(path: string): SpawnResultFile {
  const abs = resolvePath(process.cwd(), path);
  const text = readFileSync(abs, "utf-8");
  let obj: unknown;
  try {
    obj = JSON.parse(text);
  } catch (e) {
    throw new CliError(`spawn-result file ${abs} is not valid JSON: ${(e as Error).message}`);
  }
  if (typeof obj !== "object" || obj === null) {
    throw new CliError(`spawn-result file ${abs} must be a JSON object`);
  }
  const o = obj as Record<string, unknown>;
  if (typeof o.turnkeyEoa !== "string" || !/^0x[0-9a-fA-F]{40}$/.test(o.turnkeyEoa)) {
    throw new CliError(`spawn-result missing turnkeyEoa hex address`);
  }
  if (typeof o.scaAddress !== "string" || !/^0x[0-9a-fA-F]{40}$/.test(o.scaAddress)) {
    throw new CliError(`spawn-result missing scaAddress hex address`);
  }
  const out: SpawnResultFile = {
    turnkeyEoa: o.turnkeyEoa,
    scaAddress: o.scaAddress,
  };
  if (typeof o.identityId === "string") out.identityId = o.identityId;
  if (typeof o.turnkeyEoaPrivateKey === "string" && /^0x[0-9a-fA-F]{64}$/.test(o.turnkeyEoaPrivateKey)) {
    out.turnkeyEoaPrivateKey = o.turnkeyEoaPrivateKey;
  }
  return out;
}

/* ------------------------------------------------------------------ */
/*                          JSON serialisation                        */
/* ------------------------------------------------------------------ */

/** JSON-stringify a value, converting bigints to decimal strings. */
function safeJson(v: unknown): string {
  return JSON.stringify(v, (_k, val) => (typeof val === "bigint" ? val.toString() : val));
}

/* ------------------------------------------------------------------ */
/*                           Subcommands                              */
/* ------------------------------------------------------------------ */

async function runQuery(args: ParsedArgs): Promise<number> {
  const url = args.positional[1];
  if (!url) {
    throw new CliError("query subcommand needs a positional URL argument");
  }

  const vecStr = requireString(args.flags, "vec");
  let vec: number[];
  try {
    const parsed = JSON.parse(vecStr);
    if (!Array.isArray(parsed) || !parsed.every((x) => typeof x === "number")) {
      throw new Error("not an array of numbers");
    }
    vec = parsed as number[];
  } catch (e) {
    throw new CliError(`--vec must be a JSON array of numbers: ${(e as Error).message}`);
  }
  const k = Number(optString(args.flags, "k") ?? "10");
  if (!Number.isFinite(k) || k < 1) {
    throw new CliError("--k must be a positive integer");
  }

  const maxUsdc = requireString(args.flags, "max-usdc");
  const chainId = Number(optString(args.flags, "chain-id") ?? "5042002");
  if (!Number.isFinite(chainId)) {
    throw new CliError("--chain-id must be a number");
  }
  const network = optString(args.flags, "network") ?? "arc-testnet";
  const usdcAddress = (optString(args.flags, "usdc") ??
    "0x3600000000000000000000000000000000000000") as Address;
  const mode = (optString(args.flags, "mode") ?? "auto") as BatchMode;
  if (!["auto", "immediate", "manual"].includes(mode)) {
    throw new CliError(`--mode must be auto|immediate|manual, got ${mode}`);
  }
  const maxBatchSize = Number(optString(args.flags, "max-batch-size") ?? "100");
  const maxBatchAge = Number(optString(args.flags, "max-batch-age") ?? "30");
  const flushAfter = optBool(args.flags, "flush-after", false);

  // EOA: from --from-spawn-result OR --local-eoa-key, with local-eoa-key
  // taking precedence (test path).
  const spawnPath = optString(args.flags, "from-spawn-result");
  const localKey = optString(args.flags, "local-eoa-key");
  let eoa: RawEoa;
  if (localKey) {
    if (!/^0x[0-9a-fA-F]{64}$/.test(localKey)) {
      throw new CliError("--local-eoa-key must be a 32-byte hex");
    }
    // Derive address by importing viem (privateKeyToAccount).
    const { privateKeyToAccount } = await import("viem/accounts");
    const account = privateKeyToAccount(localKey as Hex);
    eoa = {
      address: account.address,
      privateKey: localKey as Hex,
      backedByTEE: false,
    };
  } else if (spawnPath) {
    const file = loadSpawnResult(spawnPath);
    if (!file.turnkeyEoaPrivateKey) {
      throw new CliError(
        `spawn-result at ${spawnPath} contains a Turnkey-backed EOA (no embedded ` +
          `private key). The Turnkey signing path is not wired in this slice. ` +
          `Use --local-eoa-key for testing, or extend signer.ts to invoke ` +
          `@turnkey/sdk-server for signTypedData. See README for details.`,
      );
    }
    eoa = {
      address: file.turnkeyEoa as Address,
      privateKey: file.turnkeyEoaPrivateKey as Hex,
      backedByTEE: false,
    };
  } else {
    throw new CliError(
      "must supply --from-spawn-result <path> or --local-eoa-key <0xkey>",
    );
  }

  process.stderr.write(
    `[bob_client] paying ${url} signer=${eoa.address} ` +
      `maxUsdc=${maxUsdc} network=${network} mode=${mode}\n`,
  );

  const client = new X402BatchClient({
    signer: eoa,
    chainId,
    network,
    usdcAddress,
    gatewayBatchMode: mode,
    maxBatchSize,
    maxBatchAgeSeconds: maxBatchAge,
  });

  try {
    const result = await client.pay(url, {
      body: { query_vec: vec, k },
      maxAmountUsdc: maxUsdc,
    });

    let settlement: Awaited<ReturnType<X402BatchClient["flush"]>> | null = null;
    if (flushAfter) {
      settlement = await client.flush("manual");
    }

    process.stdout.write(
      safeJson({
        ok: true,
        url,
        result: result.data,
        payment: result.paymentAuthorization,
        client_state: client.state,
        settlement,
      }) + "\n",
    );
    await client.close();
    return 0;
  } catch (e) {
    await client.close();
    if (e instanceof X402Error) {
      process.stdout.write(
        safeJson({
          ok: false,
          url,
          error_code: e.code,
          error: e.message,
        }) + "\n",
      );
      return 1;
    }
    throw e;
  }
}

/* ------------------------------------------------------------------ */
/*                              Main                                  */
/* ------------------------------------------------------------------ */

async function main(): Promise<number> {
  const args = parseArgs(process.argv.slice(2));
  const sub = args.positional[0];
  switch (sub) {
    case "query":
      return runQuery(args);
    case undefined:
      throw new CliError(
        "usage: bob_client.ts query <url> --vec <json> --max-usdc <amount> [...]",
      );
    default:
      throw new CliError(`unknown subcommand: ${sub}`);
  }
}

// Only run when invoked directly. ``tsx scripts/x402/bob_client.ts ...``
// matches; importing as a module does not.
const isDirectExec =
  typeof process !== "undefined" &&
  process.argv[1] !== undefined &&
  /bob_client\.ts$/.test(process.argv[1]);

if (isDirectExec) {
  main().then(
    (code) => {
      process.exit(code);
    },
    (err: unknown) => {
      const msg = err instanceof Error ? err.message : String(err);
      process.stderr.write(`[bob_client ERROR] ${msg}\n`);
      process.exit(err instanceof CliError ? 2 : 1);
    },
  );
}
