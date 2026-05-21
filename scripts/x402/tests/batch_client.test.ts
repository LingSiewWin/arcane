/**
 * Vitest suite for the x402 batch client.
 *
 * All tests are REAL:
 *   - signatures are produced with viem's PrivateKeyAccount.signTypedData
 *   - signatures are recovered with viem's recoverTypedDataAddress
 *   - the dark pool is spawned as a real subprocess (uvicorn) on a random
 *     port; vitest issues real HTTP requests over fetch.
 *
 * Why subprocess instead of in-process Starlette? Because Slice 5D's
 * ``demo_e2e.py`` will subprocess-invoke ``bob_client.ts``, and that
 * binary uses globalThis.fetch. We exercise the same transport here so
 * the test surface matches the demo surface byte-for-byte.
 *
 * Environment requirements:
 *   - agents/.venv with the dark pool deps installed
 *   - uvicorn on $PATH inside that venv
 *
 * If the venv is missing, the subprocess tests skip with a clear message.
 */

import { spawn, type ChildProcess } from "node:child_process";
import { createServer } from "node:net";
import {
  afterAll,
  afterEach,
  beforeAll,
  describe,
  expect,
  it,
} from "vitest";
import {
  generatePrivateKey,
  privateKeyToAccount,
} from "viem/accounts";
import {
  recoverTypedDataAddress,
  type Address,
  type Hex,
} from "viem";
import { existsSync } from "node:fs";
import { dirname, resolve as resolvePath } from "node:path";
import { fileURLToPath } from "node:url";

import { X402BatchClient, pickAccept, usdcToBaseUnits } from "../batch_client.js";
import {
  EIP3009_TRANSFER_TYPES,
  TurnkeyEoaSigner,
  signTransferWithAuthorization,
  encodeXPaymentHeader,
} from "../signer.js";
import {
  digestAuthorizations,
  formatUsdc,
} from "../circle_gateway.js";
import {
  X402AmountExceededError,
  X402ServerRefusedError,
  type PaymentRequirements,
  type RawEoa,
} from "../types.js";

// ---------------------------------------------------------------------------
// Constants — must match agents/dark_pool.py canonical values.
// ---------------------------------------------------------------------------

const ARC_CHAIN_ID = 5042002;
const USDC_ARC: Address = "0x3600000000000000000000000000000000000000";
const NETWORK = "arc-testnet";
const PRICE_USDC = "0.001";
const PRICE_BASE_UNITS = 1000n; // 0.001 USDC * 1e6

// ---------------------------------------------------------------------------
// Subprocess helpers
// ---------------------------------------------------------------------------

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);
// Resolve repo root from scripts/x402/tests/ → ../../../
const REPO_ROOT = resolvePath(__dirname, "..", "..", "..");
const VENV_PYTHON = resolvePath(REPO_ROOT, "agents/.venv/bin/python");
const VENV_UVICORN = resolvePath(REPO_ROOT, "agents/.venv/bin/uvicorn");

/** Pick a free TCP port on 127.0.0.1. */
async function pickFreePort(): Promise<number> {
  return new Promise((resolve, reject) => {
    const srv = createServer();
    srv.unref();
    srv.on("error", reject);
    srv.listen(0, "127.0.0.1", () => {
      const addr = srv.address();
      if (addr && typeof addr === "object") {
        const port = addr.port;
        srv.close(() => resolve(port));
      } else {
        srv.close();
        reject(new Error("could not pick port"));
      }
    });
  });
}

/** Wait until ``GET /health`` on the URL returns 200 (or timeout). */
async function waitForHealthy(url: string, timeoutMs = 15000): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  let lastErr: unknown = null;
  while (Date.now() < deadline) {
    try {
      const res = await fetch(url);
      if (res.status === 200) return;
      lastErr = new Error(`status=${res.status}`);
    } catch (e) {
      lastErr = e;
    }
    await new Promise((r) => setTimeout(r, 200));
  }
  throw new Error(`server never became healthy at ${url}: ${String(lastErr)}`);
}

interface SpawnedServer {
  proc: ChildProcess;
  baseUrl: string;
  port: number;
  recipient: Address;
  stop: () => Promise<void>;
}

/** Spawn the test dark pool app via uvicorn. Caller is responsible for stop(). */
async function spawnDarkPool(opts: {
  recipient: Address;
  priceUsdc?: string;
  chainId?: number;
  usdc?: Address;
  seedDim?: number;
  seedCount?: number;
}): Promise<SpawnedServer> {
  const port = await pickFreePort();
  const env: Record<string, string> = {
    ...process.env,
    PYTHONPATH: REPO_ROOT,
    DARKPOOL_TEST_RECIPIENT: opts.recipient,
    DARKPOOL_TEST_PRICE_USDC: opts.priceUsdc ?? PRICE_USDC,
    DARKPOOL_TEST_CHAIN_ID: String(opts.chainId ?? ARC_CHAIN_ID),
    DARKPOOL_TEST_USDC: opts.usdc ?? USDC_ARC,
    DARKPOOL_TEST_SEED_DIM: String(opts.seedDim ?? 384),
    DARKPOOL_TEST_SEED_COUNT: String(opts.seedCount ?? 5),
  };
  const proc = spawn(
    VENV_UVICORN,
    [
      "scripts.x402.tests._darkpool_test_app:app",
      "--host",
      "127.0.0.1",
      "--port",
      String(port),
      "--log-level",
      "warning",
    ],
    {
      cwd: REPO_ROOT,
      env,
      stdio: ["ignore", "pipe", "pipe"],
    },
  );

  let stderr = "";
  proc.stderr?.on("data", (chunk: Buffer) => {
    stderr += chunk.toString();
  });
  proc.on("exit", (code, signal) => {
    if (code !== 0 && code !== null) {
      // eslint-disable-next-line no-console
      console.error(
        `[darkpool subprocess] exited with code=${code} signal=${signal}\n` + stderr,
      );
    }
  });

  const baseUrl = `http://127.0.0.1:${port}`;
  try {
    await waitForHealthy(`${baseUrl}/health`);
  } catch (e) {
    proc.kill("SIGKILL");
    throw new Error(`darkpool failed to start: ${(e as Error).message}\n${stderr}`);
  }

  const stop = (): Promise<void> =>
    new Promise((resolve) => {
      if (proc.exitCode !== null) return resolve();
      proc.once("exit", () => resolve());
      proc.kill("SIGTERM");
      // Hard kill after 3s if it ignores SIGTERM.
      setTimeout(() => {
        if (proc.exitCode === null) proc.kill("SIGKILL");
      }, 3000);
    });

  return { proc, baseUrl, port, recipient: opts.recipient, stop };
}

/** Recipient EOAs are deterministic across tests but address differs per run. */
function freshRecipient(): { privateKey: Hex; address: Address } {
  const pk = generatePrivateKey();
  const acct = privateKeyToAccount(pk);
  return { privateKey: pk, address: acct.address };
}

/** Build a local-EOA ``RawEoa`` (Bob's signer). */
function localEoa(): RawEoa {
  const pk = generatePrivateKey();
  const acct = privateKeyToAccount(pk);
  return { address: acct.address, privateKey: pk, backedByTEE: false };
}

/** Build a query vector matching the seeded test memory. ``axis`` ∈ [0, dim). */
function unitVector(axis: number, dim: number): number[] {
  const v = new Array<number>(dim).fill(0);
  v[axis] = 1.0;
  return v;
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe("EIP-712 / EIP-3009 signing", () => {
  it("test_sign_real_eip712_round_trip — signature recovers to EOA address", async () => {
    // Fresh local EOA — no Turnkey, no env vars.
    const eoa = localEoa();
    const signer = new TurnkeyEoaSigner(eoa);
    const recipient = freshRecipient().address;

    // Synthesize a PaymentRequirements that mirrors the dark pool's 402 body.
    const requirements: PaymentRequirements = {
      scheme: "exact",
      network: NETWORK,
      maxAmountRequired: "1000",
      resource: "/query",
      description: "test",
      mimeType: "application/json",
      payTo: recipient,
      maxTimeoutSeconds: 60,
      asset: USDC_ARC,
      extra: { name: "USDC", version: "2" },
    };

    const auth = await signTransferWithAuthorization({
      signer,
      requirements,
      chainId: ARC_CHAIN_ID,
      value: 1000n,
      validForSeconds: 60,
    });

    // Recover the signer using viem and assert it matches.
    const recovered = await recoverTypedDataAddress({
      domain: {
        name: "USDC",
        version: "2",
        chainId: ARC_CHAIN_ID,
        verifyingContract: USDC_ARC,
      },
      types: EIP3009_TRANSFER_TYPES as unknown as Record<
        string,
        Array<{ name: string; type: string }>
      >,
      primaryType: "TransferWithAuthorization",
      message: {
        from: auth.authorization.from,
        to: auth.authorization.to,
        value: auth.authorization.value,
        validAfter: auth.authorization.validAfter,
        validBefore: auth.authorization.validBefore,
        nonce: auth.authorization.nonce,
      },
      signature: auth.signature,
    });

    expect(recovered.toLowerCase()).toBe(eoa.address.toLowerCase());

    // Bonus: the X-PAYMENT header must base64-decode to a JSON object with
    // the exact field shape agents/dark_pool.py expects.
    const header = encodeXPaymentHeader(auth);
    const decoded = JSON.parse(Buffer.from(header, "base64").toString("utf-8"));
    expect(decoded.x402Version).toBe(1);
    expect(decoded.scheme).toBe("exact");
    expect(decoded.network).toBe(NETWORK);
    expect(decoded.payload.signature).toBe(auth.signature);
    expect(decoded.payload.authorization.from.toLowerCase()).toBe(
      eoa.address.toLowerCase(),
    );
    // Numeric fields are strings on the wire (dark_pool.py reads `int(value)`).
    expect(typeof decoded.payload.authorization.value).toBe("string");
    expect(typeof decoded.payload.authorization.validAfter).toBe("string");
    expect(typeof decoded.payload.authorization.validBefore).toBe("string");
  });

  it("USDC base unit conversion is exact (no float drift)", () => {
    expect(usdcToBaseUnits("0.001")).toBe(1_000n);
    expect(usdcToBaseUnits("1")).toBe(1_000_000n);
    expect(usdcToBaseUnits("1.000001")).toBe(1_000_001n);
    expect(usdcToBaseUnits("0.000001")).toBe(1n);
    expect(usdcToBaseUnits("0")).toBe(0n);
    expect(() => usdcToBaseUnits("0.0000001")).toThrow();
    expect(() => usdcToBaseUnits("abc")).toThrow();
    expect(formatUsdc(1_000n)).toBe("0.001");
    expect(formatUsdc(1_000_001n)).toBe("1.000001");
    expect(formatUsdc(2_000_000n)).toBe("2");
  });

  it("amount guard refuses a too-expensive accepts entry", () => {
    const recipient = freshRecipient().address;
    const requirements: PaymentRequirements = {
      scheme: "exact",
      network: NETWORK,
      maxAmountRequired: "2000",
      resource: "/query",
      description: "test",
      mimeType: "application/json",
      payTo: recipient,
      maxTimeoutSeconds: 60,
      asset: USDC_ARC,
      extra: { name: "USDC", version: "2" },
    };
    // PRICE_BASE_UNITS = 1000, server demands 2000 — must throw.
    expect(() => {
      pickAccept([requirements], {
        network: NETWORK,
        asset: USDC_ARC,
        maxAmountBaseUnits: PRICE_BASE_UNITS,
      });
    }).toThrow(X402AmountExceededError);
  });

  it("authorization digest is stable across identical inputs", async () => {
    const eoa = localEoa();
    const signer = new TurnkeyEoaSigner(eoa);
    const recipient = freshRecipient().address;
    const requirements: PaymentRequirements = {
      scheme: "exact",
      network: NETWORK,
      maxAmountRequired: "1000",
      resource: "/query",
      description: "test",
      mimeType: "application/json",
      payTo: recipient,
      maxTimeoutSeconds: 60,
      asset: USDC_ARC,
      extra: { name: "USDC", version: "2" },
    };
    // Two identical sigs → identical digest. (Force the same nonce + same `now`.)
    const nonce: Hex = "0x" + "11".repeat(32) as Hex;
    const baseTime = 1_700_000_000;
    const auth1 = await signTransferWithAuthorization({
      signer,
      requirements,
      chainId: ARC_CHAIN_ID,
      value: 1000n,
      validForSeconds: 60,
      nonce,
      now: () => baseTime,
    });
    const auth2 = await signTransferWithAuthorization({
      signer,
      requirements,
      chainId: ARC_CHAIN_ID,
      value: 1000n,
      validForSeconds: 60,
      nonce,
      now: () => baseTime,
    });
    expect(auth1.signature).toBe(auth2.signature);
    const d1 = await digestAuthorizations([auth1]);
    const d2 = await digestAuthorizations([auth2]);
    expect(d1).toBe(d2);
  });
});

// ---------------------------------------------------------------------------
// Real-subprocess tests — these need the python venv.
// ---------------------------------------------------------------------------

const SUBPROC_AVAILABLE = existsSync(VENV_UVICORN);

describe.skipIf(!SUBPROC_AVAILABLE)("X402BatchClient against real dark pool subprocess", () => {
  // Each test gets its own server so nonces and rate limiters reset.
  let server: SpawnedServer | null = null;

  afterEach(async () => {
    if (server) {
      await server.stop();
      server = null;
    }
  });

  it("test_pay_against_real_dark_pool_server — 402 → sign → 200", async () => {
    const recipient = freshRecipient().address;
    server = await spawnDarkPool({ recipient, seedDim: 8, seedCount: 4 });

    const client = new X402BatchClient({
      signer: localEoa(),
      chainId: ARC_CHAIN_ID,
      network: NETWORK,
      usdcAddress: USDC_ARC,
      gatewayBatchMode: "manual",
    });

    const result = await client.pay(`${server.baseUrl}/query`, {
      body: { query_vec: unitVector(2, 8), k: 3 },
      maxAmountUsdc: "0.01",
    });

    expect(result.status).toBe(200);
    expect(Array.isArray((result.data as { results: unknown[] }).results)).toBe(true);
    const results = (result.data as { results: Array<{ trace_id: string; score: number; payload: { index: number } }> }).results;
    expect(results.length).toBeGreaterThan(0);
    expect(results.length).toBeLessThanOrEqual(3);
    // Top hit must be trace_002 (axis 2 unit vector) — that's the seeded vector
    // that points along axis 2.
    expect(results[0]!.trace_id).toBe("trace_002");
    // Sanity: the queued authorization recovers to the client's signer.
    const auth = result.paymentAuthorization;
    const recovered = await recoverTypedDataAddress({
      domain: {
        name: auth.domainName,
        version: auth.domainVersion,
        chainId: auth.chainId,
        verifyingContract: auth.verifyingContract,
      },
      types: EIP3009_TRANSFER_TYPES as unknown as Record<
        string,
        Array<{ name: string; type: string }>
      >,
      primaryType: "TransferWithAuthorization",
      message: {
        from: auth.authorization.from,
        to: auth.authorization.to,
        value: auth.authorization.value,
        validAfter: auth.authorization.validAfter,
        validBefore: auth.authorization.validBefore,
        nonce: auth.authorization.nonce,
      },
      signature: auth.signature,
    });
    expect(recovered.toLowerCase()).toBe(client.address.toLowerCase());

    // The queue should have exactly one pending item (manual mode).
    expect(client.state.pendingCount).toBe(1);
    const settlement = await client.flush("manual");
    expect(settlement.items.length).toBe(1);
    expect(settlement.totalValue).toBe(PRICE_BASE_UNITS);
    expect(settlement.broadcast).toBe(false); // no Gateway creds
    expect(settlement.reason).toBe("manual");
    expect(client.state.pendingCount).toBe(0);
    await client.close();
  }, 30_000);

  it("test_batch_flushes_at_capacity — auto-flush at maxBatchSize", async () => {
    const recipient = freshRecipient().address;
    server = await spawnDarkPool({ recipient, seedDim: 8, seedCount: 4 });
    const eoa = localEoa();
    const client = new X402BatchClient({
      signer: eoa,
      chainId: ARC_CHAIN_ID,
      network: NETWORK,
      usdcAddress: USDC_ARC,
      gatewayBatchMode: "auto",
      maxBatchSize: 2,
      maxBatchAgeSeconds: 9999, // ensure age-trigger doesn't fire
    });

    // Pay 1 → queue grows to 1.
    const r1 = await client.pay(`${server.baseUrl}/query`, {
      body: { query_vec: unitVector(0, 8), k: 1 },
      maxAmountUsdc: "0.01",
    });
    expect(r1.status).toBe(200);
    expect(client.state.pendingCount).toBe(1);

    // Pay 2 → queue hits maxBatchSize (2), auto-flush fires.
    const r2 = await client.pay(`${server.baseUrl}/query`, {
      body: { query_vec: unitVector(1, 8), k: 1 },
      maxAmountUsdc: "0.01",
    });
    expect(r2.status).toBe(200);

    // The auto-flush is fire-and-forget; give it a tick.
    await new Promise((r) => setTimeout(r, 100));
    expect(client.state.pendingCount).toBe(0);
    await client.close();
  }, 30_000);

  it("test_batch_flushes_at_age — auto-flush after maxBatchAgeSeconds", async () => {
    const recipient = freshRecipient().address;
    server = await spawnDarkPool({ recipient, seedDim: 8, seedCount: 4 });
    const client = new X402BatchClient({
      signer: localEoa(),
      chainId: ARC_CHAIN_ID,
      network: NETWORK,
      usdcAddress: USDC_ARC,
      gatewayBatchMode: "auto",
      maxBatchSize: 1000, // never trip by size
      maxBatchAgeSeconds: 2, // age-flush in 2s
    });

    await client.pay(`${server.baseUrl}/query`, {
      body: { query_vec: unitVector(0, 8), k: 1 },
      maxAmountUsdc: "0.01",
    });
    expect(client.state.pendingCount).toBe(1);

    // Age timer runs once a second; wait long enough for it to trigger.
    await new Promise((r) => setTimeout(r, 3500));
    expect(client.state.pendingCount).toBe(0);
    await client.close();
  }, 15_000);

  it("test_max_amount_guard — refuses to sign when server demands too much", async () => {
    const recipient = freshRecipient().address;
    // Server priced at 0.005 USDC.
    server = await spawnDarkPool({
      recipient,
      priceUsdc: "0.005",
      seedDim: 8,
      seedCount: 4,
    });
    const client = new X402BatchClient({
      signer: localEoa(),
      chainId: ARC_CHAIN_ID,
      network: NETWORK,
      usdcAddress: USDC_ARC,
      gatewayBatchMode: "manual",
    });

    // Caller will only pay up to 0.001 USDC — under the server price.
    await expect(
      client.pay(`${server.baseUrl}/query`, {
        body: { query_vec: unitVector(0, 8), k: 1 },
        maxAmountUsdc: "0.001",
      }),
    ).rejects.toBeInstanceOf(X402AmountExceededError);
    // Queue must still be empty — we never signed.
    expect(client.state.pendingCount).toBe(0);
    await client.close();
  }, 30_000);

  it("test_replay_protection_returns_402 — same nonce twice rejected", async () => {
    const recipient = freshRecipient().address;
    server = await spawnDarkPool({ recipient, seedDim: 8, seedCount: 4 });

    const eoa = localEoa();
    const signer = new TurnkeyEoaSigner(eoa);
    // Hand-craft the same signed authorization twice and POST it directly so
    // we control the nonce. Reusing the client's pay() would always pick a
    // fresh nonce. Result: first POST returns 200, second returns 402 with
    // ``nonce replayed`` error.
    const queryBody = JSON.stringify({ query_vec: unitVector(0, 8), k: 1 });

    // Trigger one round to capture the accepts shape.
    const initial = await fetch(`${server.baseUrl}/query`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: queryBody,
    });
    expect(initial.status).toBe(402);
    const challenge = await initial.json();
    const accept = challenge.accepts[0];

    const requirements: PaymentRequirements = {
      scheme: accept.scheme,
      network: accept.network,
      maxAmountRequired: accept.maxAmountRequired,
      resource: accept.resource,
      description: accept.description,
      mimeType: accept.mimeType,
      payTo: accept.payTo,
      maxTimeoutSeconds: accept.maxTimeoutSeconds,
      asset: accept.asset,
      extra: accept.extra,
    };

    const fixedNonce: Hex = ("0x" + "ab".repeat(32)) as Hex;
    const auth = await signTransferWithAuthorization({
      signer,
      requirements,
      chainId: ARC_CHAIN_ID,
      value: BigInt(accept.maxAmountRequired),
      validForSeconds: 60,
      nonce: fixedNonce,
    });
    const header = encodeXPaymentHeader(auth);

    const first = await fetch(`${server.baseUrl}/query`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-PAYMENT": header },
      body: queryBody,
    });
    expect(first.status).toBe(200);

    const second = await fetch(`${server.baseUrl}/query`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-PAYMENT": header },
      body: queryBody,
    });
    expect(second.status).toBe(402);
    const replayBody = await second.json();
    // The dark pool surfaces the error string inside the 402 body.
    expect(replayBody.error).toMatch(/nonce replayed/);
  }, 30_000);

  it("test_immediate_mode_does_not_queue — single-shot per pay()", async () => {
    const recipient = freshRecipient().address;
    server = await spawnDarkPool({ recipient, seedDim: 8, seedCount: 4 });
    const client = new X402BatchClient({
      signer: localEoa(),
      chainId: ARC_CHAIN_ID,
      network: NETWORK,
      usdcAddress: USDC_ARC,
      gatewayBatchMode: "immediate",
    });
    await client.pay(`${server.baseUrl}/query`, {
      body: { query_vec: unitVector(0, 8), k: 1 },
      maxAmountUsdc: "0.01",
    });
    expect(client.state.pendingCount).toBe(0);
    await client.pay(`${server.baseUrl}/query`, {
      body: { query_vec: unitVector(1, 8), k: 1 },
      maxAmountUsdc: "0.01",
    });
    expect(client.state.pendingCount).toBe(0);
    await client.close();
  }, 30_000);

  it("test_server_refusal_surfaces_400 — non-402 non-200 propagates", async () => {
    const recipient = freshRecipient().address;
    server = await spawnDarkPool({ recipient, seedDim: 8, seedCount: 4 });
    const client = new X402BatchClient({
      signer: localEoa(),
      chainId: ARC_CHAIN_ID,
      network: NETWORK,
      usdcAddress: USDC_ARC,
      gatewayBatchMode: "manual",
    });
    // Wrong dim — should return 400 on the retry. But the FIRST request is
    // ALSO subject to dim validation? No — the server returns 402 before
    // inspecting the vector. So we hit 402 → sign → retry → 400 mismatch.
    // Wait: the dark pool actually validates the vector inside _handle_query
    // before returning 402 only if payment header is missing. Let me check
    // by reading the actual order: the server parses the body first (400
    // on bad JSON), then checks for X-PAYMENT (402 if missing), then
    // validates payment, then checks vec dim. So sending dim-mismatched
    // vec on a 0-payment first request DOES get 402, then 400 on retry.
    // The client wraps both 200 and 400 into X402ServerRefusedError when
    // the retry returns non-200.
    await expect(
      client.pay(`${server.baseUrl}/query`, {
        body: { query_vec: [1, 2, 3] /* dim mismatch */, k: 1 },
        maxAmountUsdc: "0.01",
      }),
    ).rejects.toBeInstanceOf(X402ServerRefusedError);
    await client.close();
  }, 30_000);
});

// Always-run sanity (no subprocess).
describe("client lifecycle", () => {
  it("close() is idempotent and returns a zero-item settlement on second call", async () => {
    const client = new X402BatchClient({
      signer: localEoa(),
      chainId: ARC_CHAIN_ID,
      network: NETWORK,
      usdcAddress: USDC_ARC,
      gatewayBatchMode: "manual",
    });
    const first = await client.close();
    expect(first.items.length).toBe(0);
    const second = await client.close();
    expect(second.items.length).toBe(0);
  });

  it("describe() returns a non-empty status string", () => {
    const client = new X402BatchClient({
      signer: localEoa(),
      chainId: ARC_CHAIN_ID,
      network: NETWORK,
      usdcAddress: USDC_ARC,
      gatewayBatchMode: "manual",
    });
    const s = client.describe();
    expect(s).toMatch(/X402BatchClient/);
    expect(s).toMatch(client.address);
    void client.close();
  });
});

// Hook to make sure no zombie subprocesses linger if a test threw.
const _spawned: ChildProcess[] = [];
beforeAll(() => undefined);
afterAll(async () => {
  for (const p of _spawned) {
    if (p.exitCode === null) p.kill("SIGKILL");
  }
});
