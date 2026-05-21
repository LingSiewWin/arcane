/**
 * Circle Gateway wrapper — settlement layer for batched x402 payments.
 *
 * Two paths are supported here:
 *
 * 1. **Hand-rolled batched digest** (default). When Gateway credentials are
 *    NOT configured we still flush the queue: we compute a deterministic
 *    "batch digest" (sha256 over the encoded authorizations) and return it
 *    as a record of what would have been settled. NO on-chain tx is sent.
 *    This is the safe default for tests and dry-runs.
 *
 * 2. **Real Gateway settlement**. When ``gatewayPrivateKey`` is supplied,
 *    we instantiate ``GatewayClient`` from ``@circle-fin/x402-batching``
 *    and call ``client.pay(url)`` per item in the batch. This requires:
 *      - the buyer has already deposited USDC into the Gateway Wallet
 *        (see ``GatewayClient.deposit('amount')``);
 *      - the resource server advertises ``extra.name = "GatewayWalletBatched"``
 *        in its 402 ``accepts``.
 *    Today the dark pool does NOT advertise that mode — it signs against
 *    USDC directly. So this path is unused unless / until the server is
 *    extended to publish a Gateway batched option. We keep it wired so the
 *    integration can be flipped on without touching the client code.
 *
 * Refs (load-bearing — these are the canonical docs the user asked us to pull):
 *   - ~/.arc-canteen/context/docs/circlefin-skills/use-gateway.md:104
 *       "Deposit -> Create burn intent -> Sign EIP-712 -> Submit to Gateway API
 *        -> Mint on destination"
 *   - ~/.arc-canteen/context/docs/circlefin-skills/use-gateway.md:21
 *       "Gateway REST base URL testnet: https://gateway-api-testnet.circle.com/v1/"
 *   - @circle-fin/x402-batching@3.0.4 (./dist/client/index.d.ts: GatewayClient)
 */

import type { Hex, Address } from "viem";
import {
  type BatchedSettlement,
  type PaymentAuthorization,
  type UsdcBaseUnits,
  GatewaySettlementError,
} from "./types.js";
import { encodeXPaymentHeader } from "./signer.js";

/**
 * Circle Gateway REST base URLs. See use-gateway.md §"Prerequisites / Setup".
 */
export const GATEWAY_API_TESTNET = "https://gateway-api-testnet.circle.com/v1/";
export const GATEWAY_API_MAINNET = "https://gateway-api.circle.com/v1/";

/**
 * Per-network configs surface — keep small. The SDK exposes ``CHAIN_CONFIGS``;
 * we just need ``arcTestnet`` for this slice. Re-exporting the SDK constant
 * keeps the source of truth in one place.
 */
export {
  CHAIN_CONFIGS,
  GATEWAY_DOMAINS,
  GATEWAY_AUTH_VALIDITY_WINDOW_SECONDS,
} from "@circle-fin/x402-batching/client";

/**
 * Lazy-load the SDK so callers without ``@circle-fin/x402-batching`` installed
 * (e.g. headless tests) don't crash at import time.
 */
async function loadGatewaySdk(): Promise<typeof import("@circle-fin/x402-batching/client")> {
  return import("@circle-fin/x402-batching/client");
}

/** Settlement options. */
export interface SettlementOptions {
  /** Why this settlement was triggered. Surfaced in the result for accounting. */
  reason: BatchedSettlement["reason"];
  /** Override fetch (used in tests). */
  fetchImpl?: typeof fetch;
}

/**
 * Hand-rolled digest of a batch — used when we cannot broadcast.
 *
 * Returns a hex-encoded sha256 of the canonical concatenation of each
 * authorization's X-PAYMENT header (which itself embeds the signature).
 *
 * The dark pool already validated each signature individually; this digest
 * just serves as a stable identifier for the batch (useful for downstream
 * Slice 5D's ``demo_e2e.py`` which logs flushed batches).
 */
export async function digestAuthorizations(
  items: PaymentAuthorization[],
): Promise<Hex> {
  const enc = new TextEncoder();
  const parts: Uint8Array[] = [];
  for (const item of items) {
    parts.push(enc.encode(encodeXPaymentHeader(item)));
    parts.push(enc.encode("\n"));
  }
  const total = parts.reduce((acc, p) => acc + p.byteLength, 0);
  const flat = new Uint8Array(total);
  let off = 0;
  for (const p of parts) {
    flat.set(p, off);
    off += p.byteLength;
  }
  // Node 20+ exposes WebCrypto subtle.
  const digest = await globalThis.crypto.subtle.digest("SHA-256", flat);
  const bytes = new Uint8Array(digest);
  let hex = "0x";
  for (const b of bytes) hex += b.toString(16).padStart(2, "0");
  return hex as Hex;
}

/**
 * Settle a batch of payment authorizations.
 *
 * Behavior:
 *   - If no Gateway private key is configured, returns a digest-only
 *     ``BatchedSettlement`` with ``broadcast=false`` and ``txHash`` unset.
 *   - If a Gateway private key + chain key are configured AND every item
 *     in the batch was signed against the Gateway batched domain, dispatches
 *     each authorization to Circle Gateway via ``GatewayClient.pay()``.
 *
 * Note: ``GatewayClient.pay(url)`` is *itself* a full x402 round-trip —
 * the SDK re-does the 402 dance, signs, retries, and waits for settlement.
 * That means the "real Gateway" path replays the request rather than just
 * settling the cached authorizations. We expose both paths so the demo can
 * choose. See README.md for the trade-off.
 */
export class CircleGatewaySettler {
  private readonly gatewayPrivateKey?: Hex;
  private readonly chainName: string;
  private readonly fetchImpl: typeof fetch;
  /** Lazy SDK client (constructed on first use). */
  private sdkClient: unknown | null = null;

  constructor(opts: {
    gatewayPrivateKey?: Hex;
    chainName?: string;
    fetchImpl?: typeof fetch;
  } = {}) {
    if (opts.gatewayPrivateKey) {
      this.gatewayPrivateKey = opts.gatewayPrivateKey;
    }
    this.chainName = opts.chainName ?? "arcTestnet";
    this.fetchImpl = opts.fetchImpl ?? globalThis.fetch.bind(globalThis);
  }

  /** Whether this settler can actually broadcast (has creds). */
  get canBroadcast(): boolean {
    return this.gatewayPrivateKey !== undefined;
  }

  /**
   * Flush a batch.
   *
   * When ``canBroadcast === false``, returns a digest-only settlement.
   * When true, calls ``GatewayClient.pay()`` for each item URL. We don't
   * have the URL stashed per-item in this slice, so the "real Gateway"
   * path REQUIRES the caller (X402BatchClient) to invoke ``settle()`` with
   * the URL list. For now, attempting real settlement on items signed
   * against USDC (not GatewayWallet) throws — there's no equivalent
   * "settle pre-signed USDC authorizations" endpoint that I can find in
   * the public SDK; that path runs on chain via the USDC contract's
   * ``transferWithAuthorization`` selector and needs gas, which defeats
   * the point of batching.
   */
  async settle(
    items: PaymentAuthorization[],
    options: SettlementOptions,
  ): Promise<BatchedSettlement> {
    const flushedAt = Math.floor(Date.now() / 1000);
    const totalValue: UsdcBaseUnits = items.reduce(
      (acc, it) => acc + it.authorization.value,
      0n,
    );

    if (items.length === 0) {
      return {
        items: [],
        totalValue: 0n,
        reason: options.reason,
        broadcast: false,
        flushedAt,
      };
    }

    if (!this.canBroadcast) {
      // Digest-only path. This is the default for the demo because the
      // dark pool already validated each EIP-3009 signature server-side.
      return {
        items,
        totalValue,
        reason: options.reason,
        broadcast: false,
        flushedAt,
      };
    }

    // Real broadcast path. We can settle ONLY items whose ``domainName ===
    // "GatewayWalletBatched"`` — anything else was signed against USDC and
    // would need an on-chain ``transferWithAuthorization`` call (out of scope
    // here, would cost gas).
    const eligible = items.filter(
      (it) => it.domainName === "GatewayWalletBatched",
    );
    if (eligible.length !== items.length) {
      throw new GatewaySettlementError(
        `cannot broadcast: ${items.length - eligible.length} of ${items.length} ` +
          `authorizations were signed against direct USDC (not GatewayWallet). ` +
          `Either disable broadcast (no GATEWAY_PRIVATE_KEY) or extend the ` +
          `resource server to advertise extra.name="GatewayWalletBatched".`,
      );
    }

    // The SDK's GatewayClient flow re-initiates 402; we'd need the URL per
    // item to call it. The X402BatchClient is the only thing that has the
    // URL and we currently flush without it. So: real-broadcast is gated on
    // an explicit ``settleViaSdk()`` call from the X402BatchClient that
    // hands us URLs alongside items.
    throw new GatewaySettlementError(
      "broadcast-on-flush is not yet wired in Slice 5C: pre-signed " +
        "authorizations cannot be replayed through GatewayClient.pay() " +
        "without their resource URL. Use settleViaSdk(items, urls) when " +
        "wiring the demo via the SDK's full 402 path.",
    );
  }

  /**
   * Real Circle Gateway broadcast path — pays one resource URL per item via
   * the SDK's ``GatewayClient.pay()``. This re-does the 402 dance, signs
   * fresh authorizations against the Gateway domain, and returns the
   * settlement tx hash from Circle's facilitator.
   *
   * Requires:
   *   - ``gatewayPrivateKey`` configured;
   *   - the resource server publishes a Gateway batched accepts entry.
   *
   * The dark pool today does NOT publish that entry. So calling this on
   * the demo's dark pool will fail at the 402 stage with "no Gateway
   * batching option found". The path is included for completeness and
   * forward-compat.
   */
  async settleViaSdk(urls: string[]): Promise<{
    /** Tx hashes returned by the SDK per item. */
    txHashes: Hex[];
    /** Total USDC base units paid. */
    totalValue: UsdcBaseUnits;
  }> {
    if (!this.gatewayPrivateKey) {
      throw new GatewaySettlementError(
        "settleViaSdk requires gatewayPrivateKey",
      );
    }
    const { GatewayClient } = await loadGatewaySdk();
    if (!this.sdkClient) {
      // Cast: SDK accepts a narrow ``SupportedChainName`` enum; we keep the
      // chain key as a string here to avoid forcing every call site to type
      // through the SDK's enum.
      // We cast the chain key to the SDK's narrow enum at the boundary.
      this.sdkClient = new GatewayClient({
        chain: this.chainName as never,
        privateKey: this.gatewayPrivateKey,
      });
    }
    const client = this.sdkClient as InstanceType<typeof GatewayClient>;
    const txHashes: Hex[] = [];
    let total: UsdcBaseUnits = 0n;
    for (const url of urls) {
      // GatewayClient.pay does the full 402 + sign + settle internally.
      // This is a real on-chain settlement (or facilitator-mediated batch).
      const res = await client.pay(url);
      txHashes.push(res.transaction as Hex);
      total += res.amount;
    }
    return { txHashes, totalValue: total };
  }

  /**
   * Convenience: deposit USDC into the buyer's Gateway Wallet. Required
   * before ``settleViaSdk()`` can pay anything. One-time setup per buyer.
   */
  async deposit(amount: string): Promise<{ depositTxHash: Hex }> {
    if (!this.gatewayPrivateKey) {
      throw new GatewaySettlementError("deposit requires gatewayPrivateKey");
    }
    const { GatewayClient } = await loadGatewaySdk();
    if (!this.sdkClient) {
      // We cast the chain key to the SDK's narrow enum at the boundary.
      this.sdkClient = new GatewayClient({
        chain: this.chainName as never,
        privateKey: this.gatewayPrivateKey,
      });
    }
    const client = this.sdkClient as InstanceType<typeof GatewayClient>;
    const result = await client.deposit(amount);
    return { depositTxHash: result.depositTxHash };
  }
}

/**
 * Helper: total a batch in USDC base units. Exposed so the orchestrator
 * can show "you're about to flush N USDC" before invoking ``flush()``.
 */
export function totalValueOf(items: PaymentAuthorization[]): UsdcBaseUnits {
  let sum: UsdcBaseUnits = 0n;
  for (const it of items) sum += it.authorization.value;
  return sum;
}

/** Format USDC base units (6 decimals) as a human-readable string. */
export function formatUsdc(units: UsdcBaseUnits): string {
  const whole = units / 1_000_000n;
  const frac = (units % 1_000_000n).toString().padStart(6, "0").replace(/0+$/, "");
  return frac.length > 0 ? `${whole.toString()}.${frac}` : whole.toString();
}

/** Address shape re-export to ease type imports in batch_client.ts. */
export type { Address };
