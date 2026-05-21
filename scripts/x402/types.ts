/**
 * Shared types for the x402 batching client slice (Phase 2 / Slice 5C).
 *
 * Stack:
 *   Bob's Turnkey EOA (raw ecrecover signer)
 *     -> EIP-3009 TransferWithAuthorization signature
 *       -> X-PAYMENT header sent to agents/dark_pool.py (FastAPI)
 *         -> server validates signature, returns top-k results
 *         -> our local queue accumulates authorizations
 *           -> flush() either:
 *                a. sends batched authorizations to Circle Gateway (real
 *                   settlement on Arc), OR
 *                b. returns an in-memory ``BatchedSettlement`` digest when
 *                   Gateway credentials are not configured.
 *
 * NOTE: the dark pool today signs against the USDC contract (chainId 5042002,
 * verifyingContract = 0x3600...) as the EIP-712 verifyingContract. That is
 * the "direct" x402 mode (compatible with the ``@x402/core`` exact scheme).
 * Circle Gateway's batched scheme uses ``GatewayWallet`` as verifyingContract
 * with ``extra.name = "GatewayWalletBatched"``. We support both — the server's
 * 402 ``accepts.extra`` determines which domain we sign against.
 *
 * Refs:
 *   - ~/.arc-canteen/context/docs/circlefin-skills/use-gateway.md
 *   - ~/.arc-canteen/context/docs/circlefin-skills/use-usdc.md
 *   - ~/.arc-canteen/context/docs/circlefin-skills/use-arc.md
 *   - @circle-fin/x402-batching@3.0.4
 *   - agents/dark_pool.py  (canonical server impl)
 */

import type { Address, Hex } from "viem";

/** Address shape (viem alias). */
export type EvmAddress = Address;

/** Hex string (32-byte hashes, calldata, signatures). */
export type HexString = Hex;

/** 32-byte hex (`0x` + 64 hex chars). */
export type Bytes32 = `0x${string}`;

/** USDC base units (6 decimals). Stored as ``bigint`` to avoid float drift. */
export type UsdcBaseUnits = bigint;

/**
 * The EIP-3009 ``TransferWithAuthorization`` struct.
 * Fields ordered to match the EIP-712 type definition the dark pool uses
 * (see ``_TRANSFER_WITH_AUTH_TYPES`` in agents/dark_pool.py).
 */
export interface TransferWithAuthorization {
  from: EvmAddress;
  to: EvmAddress;
  value: UsdcBaseUnits;
  validAfter: bigint;
  validBefore: bigint;
  nonce: HexString;
}

/**
 * A signed EIP-3009 authorization, ready to attach to a request or
 * submit to a settlement facilitator (Circle Gateway).
 */
export interface PaymentAuthorization {
  /** The signed struct. */
  authorization: TransferWithAuthorization;
  /** 65-byte secp256k1 signature (r||s||v). */
  signature: HexString;
  /** Network identifier echoed back from the server's 402 (e.g. ``"arc-testnet"``). */
  network: string;
  /** Scheme identifier — currently always ``"exact"``. */
  scheme: string;
  /** USDC ERC-20 (direct mode) or GatewayWallet (batched mode) — the EIP-712 verifyingContract. */
  verifyingContract: EvmAddress;
  /** Chain id used for the EIP-712 domain. */
  chainId: number;
  /** The EIP-712 domain ``name`` ("USDC" for direct, "GatewayWalletBatched" for Gateway). */
  domainName: string;
  /** The EIP-712 domain ``version`` ("2" for USDC v2 on Arc, "1" for Gateway batched). */
  domainVersion: string;
  /** Recipient (server's payTo). */
  payTo: EvmAddress;
  /** Asset contract address (USDC) — distinct from verifyingContract in batched mode. */
  asset: EvmAddress;
  /** Resource that was paid for (URL path). Helpful for accounting. */
  resource: string;
  /** Time we signed (epoch seconds). */
  signedAt: number;
}

/**
 * The single ``accepts[]`` entry the dark pool returns in a 402.
 * Mirrors the JSON shape produced by ``_PaymentRequirements.to_dict`` in
 * agents/dark_pool.py.
 */
export interface PaymentRequirements {
  scheme: string;
  network: string;
  /** USDC base units, as decimal string. */
  maxAmountRequired: string;
  resource: string;
  description: string;
  mimeType: string;
  payTo: EvmAddress;
  maxTimeoutSeconds: number;
  asset: EvmAddress;
  /** Server-supplied EIP-712 domain hints — ``{ name, version }`` for USDC,
   *  ``{ name: "GatewayWalletBatched", version: "1", verifyingContract }``
   *  for Circle batched. */
  extra: Record<string, unknown>;
}

/** Server's 402 response body. */
export interface X402Challenge {
  x402Version: number;
  accepts: PaymentRequirements[];
  error?: string;
}

/** The server's 200 body shape for ``/query``. */
export interface DarkPoolQueryResponse {
  results: Array<{
    trace_id: string;
    score: number;
    payload: Record<string, unknown>;
  }>;
}

/** Result of a successful ``pay()`` call. */
export interface PayResult<T = unknown> {
  /** Decoded JSON response body. */
  data: T;
  /** The signed payment we attached. */
  paymentAuthorization: PaymentAuthorization;
  /** HTTP status code (200 on success). */
  status: number;
}

/**
 * Result of flushing the in-memory authorization queue.
 *
 * - ``txHash`` is populated when we actually broadcast the batched
 *   settlement on Arc (gated on Circle Gateway credentials).
 * - ``items`` is the list of authorizations that were flushed.
 */
export interface BatchedSettlement {
  items: PaymentAuthorization[];
  txHash?: HexString;
  /** Total value across all authorizations (USDC base units). */
  totalValue: UsdcBaseUnits;
  /** Reason this flush was triggered. */
  reason: "manual" | "size" | "age" | "shutdown";
  /** Whether we broadcast to Circle Gateway (true) or just digested locally (false). */
  broadcast: boolean;
  /** When the flush completed (epoch seconds). */
  flushedAt: number;
}

/**
 * Batching policy.
 *
 * - ``immediate``: every pay() flushes synchronously (no batching).
 * - ``manual``: pay() enqueues; the caller must invoke ``flush()`` explicitly.
 * - ``auto``: pay() enqueues; flush is triggered automatically when
 *   ``size >= maxBatchSize`` OR the oldest item is older than
 *   ``maxBatchAgeSeconds``.
 */
export type BatchMode = "immediate" | "manual" | "auto";

/** Minimal raw EOA shape — compatible with ``scripts/wallet/types.ts:TurnkeyEoa``.
 *
 * We re-declare a minimal interface so this module compiles independently of
 * Slice 3. Either a Turnkey-backed EOA (``backedByTEE=true``, no privateKey)
 * or a local random EOA (``backedByTEE=false``, ``privateKey`` present).
 *
 * For Phase 2 Slice 5C, the only path that actually signs is the local
 * privateKey path; a real Turnkey backed EOA needs a Turnkey signer (Slice 3
 * exposes that, gated on env vars). We accept the interface either way and
 * surface a clear error if the privateKey is missing.
 */
export interface RawEoa {
  address: EvmAddress;
  privateKey?: HexString;
  turnkeyWalletId?: string;
  backedByTEE: boolean;
}

/** Config for ``X402BatchClient``. */
export interface X402BatchClientConfig {
  signer: RawEoa;
  chainId: number;
  /** Network identifier used to filter the server's accepts (e.g. ``"arc-testnet"``). */
  network: string;
  /** USDC ERC-20 address (used as verifyingContract in direct mode). */
  usdcAddress: EvmAddress;
  /** Default batch policy. */
  gatewayBatchMode?: BatchMode;
  /** Max number of authorizations to accumulate before auto-flush. */
  maxBatchSize?: number;
  /** Max age (seconds) of the oldest authorization before auto-flush. */
  maxBatchAgeSeconds?: number;
  /** When set, calls Circle Gateway's batched settlement API on flush. */
  gatewayPrivateKey?: HexString;
  /** Override the Gateway client's chain key (defaults to ``"arcTestnet"``). */
  gatewayChainName?: string;
  /** Optional override for fetch (used in tests). */
  fetchImpl?: typeof fetch;
}

/** Public, mostly-readonly view of the client's state. */
export interface X402BatchClientState {
  signerAddress: EvmAddress;
  network: string;
  chainId: number;
  usdcAddress: EvmAddress;
  pendingCount: number;
  pendingTotalValue: UsdcBaseUnits;
  oldestPendingAgeSeconds: number;
  batchMode: BatchMode;
  maxBatchSize: number;
  maxBatchAgeSeconds: number;
}

/** Options for a single ``pay()`` call. */
export interface PayOptions {
  /** Request body (object — will be JSON-stringified). */
  body?: unknown;
  /** HTTP method (default ``POST``, since the dark pool uses POST). */
  method?: "GET" | "POST" | "PUT" | "DELETE";
  /** Additional headers (merged with ``X-PAYMENT``). */
  headers?: Record<string, string>;
  /**
   * Max USDC we are willing to pay, as a decimal string (e.g. ``"0.001"``).
   * If the server's ``maxAmountRequired`` exceeds this, ``pay()`` throws
   * ``X402AmountExceededError`` BEFORE signing.
   */
  maxAmountUsdc: string;
  /** Override the batch mode for just this call. */
  batchMode?: BatchMode;
  /** Authorization validity window (seconds). Default = server's maxTimeoutSeconds. */
  validForSeconds?: number;
}

// ---------------------------------------------------------------------------
// Errors
// ---------------------------------------------------------------------------

/** Base error class for all x402 batching errors. */
export class X402Error extends Error {
  public readonly code: string;
  constructor(code: string, message: string) {
    super(message);
    this.name = "X402Error";
    this.code = code;
  }
}

/** Thrown when the server demands more than the caller's max. */
export class X402AmountExceededError extends X402Error {
  constructor(requested: bigint, allowed: bigint) {
    super(
      "AMOUNT_EXCEEDED",
      `server demanded ${requested.toString()} base units, ` +
        `caller cap is ${allowed.toString()} base units`,
    );
    this.name = "X402AmountExceededError";
  }
}

/** Thrown when no accepts entry matches our policy. */
export class X402NoAcceptableRequirementError extends X402Error {
  constructor(reason: string) {
    super("NO_ACCEPTABLE_REQUIREMENT", reason);
    this.name = "X402NoAcceptableRequirementError";
  }
}

/** Thrown when the server returns 402 after we paid. */
export class X402ServerRefusedError extends X402Error {
  public readonly status: number;
  public readonly body: string;
  constructor(status: number, body: string) {
    super("SERVER_REFUSED", `server refused after payment: ${status} ${body.slice(0, 200)}`);
    this.name = "X402ServerRefusedError";
    this.status = status;
    this.body = body;
  }
}

/** Thrown when settlement against Circle Gateway fails. */
export class GatewaySettlementError extends X402Error {
  constructor(reason: string) {
    super("GATEWAY_SETTLEMENT_FAILED", reason);
    this.name = "GatewaySettlementError";
  }
}

/** Thrown when the raw EOA lacks a usable private key (Turnkey-only EOAs). */
export class SignerUnusableError extends X402Error {
  constructor(reason: string) {
    super("SIGNER_UNUSABLE", reason);
    this.name = "SignerUnusableError";
  }
}
