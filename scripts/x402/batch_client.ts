/**
 * X402BatchClient — the TypeScript client Bob uses to query the dark pool.
 *
 * Wraps the HTTP-402 dance described in
 * ``docs/superpowers/specs/2026-05-21-constrained-cognition-design.md`` §8:
 *
 *   1. POST request body (no payment).
 *   2. Server returns 402 with ``accepts[]`` (EIP-712 typed-data spec).
 *   3. We pick a matching accepts entry, refuse if it costs more than
 *      ``maxAmountUsdc``, otherwise sign EIP-3009 ``TransferWithAuthorization``.
 *   4. Re-POST with the base64-encoded X-PAYMENT header. Return the 200 body.
 *
 * On top of the dance we maintain an in-process **queue of paid
 * authorizations**. Depending on ``gatewayBatchMode``:
 *
 *   - ``immediate`` — never queue; each pay() is a one-shot, the
 *     authorization is immediately "settled" (digested) and returned.
 *   - ``manual`` — queue every authorization. Caller must call ``flush()``
 *     to drain.
 *   - ``auto`` — queue, but trigger an automatic flush when the queue hits
 *     ``maxBatchSize`` or the oldest item ages past ``maxBatchAgeSeconds``.
 *
 * "Flush" today means "compute a stable digest of the batch and hand it to
 * Circle Gateway IF we have credentials, otherwise return digest-only".
 * See ``circle_gateway.ts`` for the trade-off.
 *
 * Refs:
 *   - agents/dark_pool.py (canonical server)
 *   - agents/x402_client.py (canonical Python client; we mirror its
 *     ``X-PAYMENT`` wire format byte-for-byte)
 *   - @circle-fin/x402-batching@3.0.4
 */

import {
  type Address,
  type Hex,
} from "viem";
import {
  type BatchedSettlement,
  type BatchMode,
  type DarkPoolQueryResponse,
  type PayOptions,
  type PayResult,
  type PaymentAuthorization,
  type PaymentRequirements,
  type RawEoa,
  type UsdcBaseUnits,
  type X402BatchClientConfig,
  type X402BatchClientState,
  type X402Challenge,
  X402AmountExceededError,
  X402Error,
  X402NoAcceptableRequirementError,
  X402ServerRefusedError,
} from "./types.js";
import {
  TurnkeyEoaSigner,
  encodeXPaymentHeader,
  signTransferWithAuthorization,
} from "./signer.js";
import {
  CircleGatewaySettler,
  formatUsdc,
  totalValueOf,
} from "./circle_gateway.js";

const DEFAULT_BATCH_MODE: BatchMode = "auto";
const DEFAULT_MAX_BATCH_SIZE = 100;
const DEFAULT_MAX_BATCH_AGE_SECONDS = 30;
const USDC_DECIMALS = 6;

/** Convert a decimal-string USDC amount to base units (6 decimals). */
export function usdcToBaseUnits(amount: string): UsdcBaseUnits {
  // Parse without floats — keep precision exact.
  const trimmed = amount.trim();
  if (!/^-?\d+(?:\.\d+)?$/.test(trimmed)) {
    throw new X402Error("INVALID_USDC_AMOUNT", `not a decimal: ${amount}`);
  }
  const negative = trimmed.startsWith("-");
  const abs = negative ? trimmed.slice(1) : trimmed;
  const dot = abs.indexOf(".");
  let whole = abs;
  let frac = "";
  if (dot !== -1) {
    whole = abs.slice(0, dot);
    frac = abs.slice(dot + 1);
  }
  if (frac.length > USDC_DECIMALS) {
    throw new X402Error(
      "INVALID_USDC_AMOUNT",
      `more than 6 decimal places: ${amount}`,
    );
  }
  const padded = frac.padEnd(USDC_DECIMALS, "0");
  const units = BigInt(whole === "" ? "0" : whole) * 10n ** BigInt(USDC_DECIMALS) + BigInt(padded === "" ? "0" : padded);
  return negative ? -units : units;
}

/** Pick a matching ``accepts[]`` entry. */
export function pickAccept(
  accepts: PaymentRequirements[],
  policy: {
    network: string;
    asset?: Address;
    maxAmountBaseUnits: UsdcBaseUnits;
  },
): PaymentRequirements {
  if (accepts.length === 0) {
    throw new X402NoAcceptableRequirementError("server returned empty accepts[]");
  }
  for (const entry of accepts) {
    if (entry.scheme !== "exact") continue;
    if (entry.network !== policy.network) continue;
    if (policy.asset && entry.asset.toLowerCase() !== policy.asset.toLowerCase()) {
      continue;
    }
    const maxRequired = BigInt(entry.maxAmountRequired);
    if (maxRequired > policy.maxAmountBaseUnits) {
      // Surface a precise error instead of silently skipping — the demo
      // would prefer a noisy refusal here so we don't accidentally
      // overpay.
      throw new X402AmountExceededError(maxRequired, policy.maxAmountBaseUnits);
    }
    return entry;
  }
  throw new X402NoAcceptableRequirementError(
    `no accepts entry matched scheme=exact network=${policy.network}` +
      (policy.asset ? ` asset=${policy.asset}` : ""),
  );
}

/**
 * Internal queue entry — wraps a ``PaymentAuthorization`` with the resource
 * URL we paid for (needed for the SDK settlement path).
 */
interface QueueEntry {
  auth: PaymentAuthorization;
  url: string;
  enqueuedAt: number;
}

/**
 * X402BatchClient — main entrypoint.
 *
 * Lifecycle:
 *   - new X402BatchClient(config)
 *   - await client.pay(url, options) // many times
 *   - await client.flush()           // drains queue, returns BatchedSettlement
 *   - await client.close()           // cancels the timer
 *
 * Thread-safety: single Node event loop; no cross-thread state. The queue
 * is mutated only inside ``pay()`` / ``flush()`` / the age-check timer
 * callback, all of which run on the main loop.
 */
export class X402BatchClient {
  public readonly signer: TurnkeyEoaSigner;
  public readonly chainId: number;
  public readonly network: string;
  public readonly usdcAddress: Address;
  public readonly batchMode: BatchMode;
  public readonly maxBatchSize: number;
  public readonly maxBatchAgeSeconds: number;
  private readonly fetchImpl: typeof fetch;
  private readonly settler: CircleGatewaySettler;
  /** Pending paid authorizations awaiting flush. */
  private queue: QueueEntry[] = [];
  /** Timer that periodically checks oldest-item age. */
  private ageTimer: ReturnType<typeof setInterval> | null = null;
  /** Whether ``close()`` has been called. */
  private closed = false;
  /** A serialization mutex for flush() so concurrent calls don't double-emit. */
  private flushInFlight: Promise<BatchedSettlement> | null = null;

  constructor(config: X402BatchClientConfig) {
    this.signer = new TurnkeyEoaSigner(config.signer);
    this.chainId = config.chainId;
    this.network = config.network;
    this.usdcAddress = config.usdcAddress;
    this.batchMode = config.gatewayBatchMode ?? DEFAULT_BATCH_MODE;
    this.maxBatchSize = config.maxBatchSize ?? DEFAULT_MAX_BATCH_SIZE;
    this.maxBatchAgeSeconds =
      config.maxBatchAgeSeconds ?? DEFAULT_MAX_BATCH_AGE_SECONDS;
    this.fetchImpl = config.fetchImpl ?? globalThis.fetch.bind(globalThis);
    const settlerOpts: ConstructorParameters<typeof CircleGatewaySettler>[0] = {
      chainName: config.gatewayChainName ?? "arcTestnet",
      fetchImpl: this.fetchImpl,
    };
    if (config.gatewayPrivateKey) {
      settlerOpts.gatewayPrivateKey = config.gatewayPrivateKey;
    }
    this.settler = new CircleGatewaySettler(settlerOpts);

    if (this.batchMode === "auto") {
      this.startAgeTimer();
    }
  }

  /** Bob's signer address. */
  get address(): Address {
    return this.signer.address;
  }

  /** Public read-only state snapshot. */
  get state(): X402BatchClientState {
    const now = Math.floor(Date.now() / 1000);
    const oldest = this.queue[0]?.enqueuedAt ?? now;
    return {
      signerAddress: this.signer.address,
      network: this.network,
      chainId: this.chainId,
      usdcAddress: this.usdcAddress,
      pendingCount: this.queue.length,
      pendingTotalValue: totalValueOf(this.queue.map((e) => e.auth)),
      oldestPendingAgeSeconds: this.queue.length > 0 ? now - oldest : 0,
      batchMode: this.batchMode,
      maxBatchSize: this.maxBatchSize,
      maxBatchAgeSeconds: this.maxBatchAgeSeconds,
    };
  }

  /**
   * Pay for a resource. Implements the full 402 round-trip and queues the
   * resulting authorization for batched settlement.
   */
  async pay<T = DarkPoolQueryResponse>(
    url: string,
    options: PayOptions,
  ): Promise<PayResult<T>> {
    if (this.closed) {
      throw new X402Error("CLOSED", "client has been closed");
    }
    const method = options.method ?? "POST";
    const bodyJson = options.body !== undefined ? JSON.stringify(options.body) : undefined;
    const maxUnits = usdcToBaseUnits(options.maxAmountUsdc);

    // 1. First request — no payment. We expect a 402.
    const baseHeaders: Record<string, string> = {
      "Content-Type": "application/json",
      ...(options.headers ?? {}),
    };
    const first = await this.fetchImpl(url, {
      method,
      headers: baseHeaders,
      ...(bodyJson !== undefined && { body: bodyJson }),
    });

    // Edge: server returned 200 immediately (free endpoint). Just return.
    if (first.status === 200) {
      const data = (await first.json()) as T;
      throw new X402Error(
        "UNEXPECTED_FREE",
        "server returned 200 without 402 — caller expected a paid resource. " +
          "Got: " + JSON.stringify(data).slice(0, 200),
      );
    }
    if (first.status !== 402) {
      const text = await first.text();
      throw new X402ServerRefusedError(first.status, text);
    }

    let challenge: X402Challenge;
    try {
      challenge = (await first.json()) as X402Challenge;
    } catch (e) {
      throw new X402Error(
        "INVALID_402_BODY",
        `402 body was not JSON: ${(e as Error).message}`,
      );
    }
    if (!challenge.accepts || !Array.isArray(challenge.accepts)) {
      throw new X402Error("INVALID_402_BODY", "402 body missing accepts[]");
    }

    // 2. Pick + validate the accepts entry.
    const accept = pickAccept(challenge.accepts, {
      network: this.network,
      asset: this.usdcAddress,
      maxAmountBaseUnits: maxUnits,
    });

    // 3. Sign EIP-3009.
    const validForSeconds =
      options.validForSeconds ?? Math.max(15, accept.maxTimeoutSeconds);
    const auth = await signTransferWithAuthorization({
      signer: this.signer,
      requirements: accept,
      chainId: this.chainId,
      value: BigInt(accept.maxAmountRequired),
      validForSeconds,
    });

    // 4. Retry with X-PAYMENT.
    const header = encodeXPaymentHeader(auth);
    const second = await this.fetchImpl(url, {
      method,
      headers: { ...baseHeaders, "X-PAYMENT": header },
      ...(bodyJson !== undefined && { body: bodyJson }),
    });

    if (second.status !== 200) {
      const text = await second.text();
      throw new X402ServerRefusedError(second.status, text);
    }
    const data = (await second.json()) as T;

    // 5. Queue / flush per batchMode policy.
    const effectiveMode = options.batchMode ?? this.batchMode;
    if (effectiveMode === "immediate") {
      // Settle synchronously — single-item batch.
      await this.settler.settle([auth], { reason: "manual" });
    } else {
      this.enqueue(auth, url);
      if (effectiveMode === "auto") {
        // Synchronous size check. Age check is handled by the timer.
        if (this.queue.length >= this.maxBatchSize) {
          // Fire-and-forget; pay() returns immediately. Errors surface
          // on the next flush() call via the rejected ``flushInFlight``.
          void this.flush("size").catch((e) => {
            // We swallow here because pay() already succeeded — the
            // failure is observable via state.pendingCount and the next
            // flush() call.
            // eslint-disable-next-line no-console
            console.error("[X402BatchClient] auto-flush failed:", e);
          });
        }
      }
    }

    return {
      data,
      paymentAuthorization: auth,
      status: 200,
    };
  }

  /** Add an authorization to the queue. Captures enqueue timestamp. */
  private enqueue(auth: PaymentAuthorization, url: string): void {
    this.queue.push({
      auth,
      url,
      enqueuedAt: Math.floor(Date.now() / 1000),
    });
  }

  /**
   * Drain the queue. Returns the ``BatchedSettlement`` describing what
   * was flushed. Safe to call when queue is empty (returns a zero-item
   * settlement).
   */
  async flush(reason: BatchedSettlement["reason"] = "manual"): Promise<BatchedSettlement> {
    // Serialize concurrent flushes — second caller awaits the first.
    if (this.flushInFlight) {
      return this.flushInFlight;
    }
    const promise = this.doFlush(reason);
    this.flushInFlight = promise;
    try {
      return await promise;
    } finally {
      // Clear AFTER awaiting so a second concurrent call always sees the
      // in-flight promise.
      this.flushInFlight = null;
    }
  }

  private async doFlush(
    reason: BatchedSettlement["reason"],
  ): Promise<BatchedSettlement> {
    const drained = this.queue;
    this.queue = [];
    const items = drained.map((e) => e.auth);
    return this.settler.settle(items, { reason });
  }

  /**
   * Start a periodic check that flushes when oldest item exceeds
   * ``maxBatchAgeSeconds``. Runs once a second by default — cheap.
   */
  private startAgeTimer(): void {
    if (this.ageTimer !== null) return;
    this.ageTimer = setInterval(() => {
      void this.maybeFlushByAge();
    }, 1000);
    // Don't block Node's exit on the timer (this is a long-lived helper,
    // not the reason the process should stay alive).
    if (typeof this.ageTimer === "object" && this.ageTimer !== null) {
      const maybeUnref = (this.ageTimer as unknown as { unref?: () => void }).unref;
      if (typeof maybeUnref === "function") {
        maybeUnref.call(this.ageTimer);
      }
    }
  }

  private async maybeFlushByAge(): Promise<void> {
    if (this.queue.length === 0) return;
    const oldest = this.queue[0]?.enqueuedAt ?? Math.floor(Date.now() / 1000);
    const age = Math.floor(Date.now() / 1000) - oldest;
    if (age >= this.maxBatchAgeSeconds) {
      try {
        await this.flush("age");
      } catch (e) {
        // eslint-disable-next-line no-console
        console.error("[X402BatchClient] age-flush failed:", e);
      }
    }
  }

  /** Stop the age timer and flush remaining items. */
  async close(): Promise<BatchedSettlement> {
    if (this.closed) {
      return {
        items: [],
        totalValue: 0n,
        reason: "shutdown",
        broadcast: false,
        flushedAt: Math.floor(Date.now() / 1000),
      };
    }
    this.closed = true;
    if (this.ageTimer !== null) {
      clearInterval(this.ageTimer);
      this.ageTimer = null;
    }
    return this.flush("shutdown");
  }

  /** Hand to ``demo_e2e.py`` for a human-readable status line. */
  describe(): string {
    const s = this.state;
    return (
      `X402BatchClient signer=${s.signerAddress} network=${s.network} ` +
      `pending=${s.pendingCount} totalValue=${formatUsdc(s.pendingTotalValue)} USDC ` +
      `oldestAge=${s.oldestPendingAgeSeconds}s mode=${s.batchMode}`
    );
  }
}

// Re-exports for ergonomic single-import use.
export type {
  PayOptions,
  PayResult,
  PaymentAuthorization,
  BatchedSettlement,
  BatchMode,
} from "./types.js";
export { TurnkeyEoaSigner, encodeXPaymentHeader } from "./signer.js";
export { CircleGatewaySettler, formatUsdc } from "./circle_gateway.js";
export type { Hex };
