/**
 * EIP-712 / EIP-3009 signer adapter for the x402 batch client.
 *
 * Given a raw Turnkey EOA (or local random EOA fallback) from
 * ``scripts/wallet/turnkey_client.ts``, this module produces a signer
 * surface compatible with both:
 *
 *   1. ``@circle-fin/x402-batching``'s ``BatchEvmSigner`` interface
 *      (used when the server advertises ``extra.name = "GatewayWalletBatched"``).
 *   2. The direct EIP-3009 ``TransferWithAuthorization`` flow that
 *      ``agents/dark_pool.py`` actually speaks today (signs against the
 *      USDC ERC-20 contract on Arc).
 *
 * The verifyingContract is decided per-call, NOT hardcoded — the server's
 * 402 ``accepts.extra.verifyingContract`` (Gateway mode) or
 * ``accepts.asset`` (direct mode) tells us which domain to sign against.
 *
 * Tested via real signature recovery in ``tests/batch_client.test.ts`` —
 * the test asserts ``recoverTypedDataAddress(signed) === eoa.address``.
 */

import {
  privateKeyToAccount,
  type PrivateKeyAccount,
} from "viem/accounts";
import type { Hex, Address } from "viem";
import {
  type PaymentAuthorization,
  type PaymentRequirements,
  type RawEoa,
  type TransferWithAuthorization,
  type UsdcBaseUnits,
  SignerUnusableError,
  X402AmountExceededError,
} from "./types.js";

/**
 * Canonical EIP-712 type definitions for EIP-3009 TransferWithAuthorization.
 *
 * NOTE: field order MUST match the dark pool's ``_TRANSFER_WITH_AUTH_TYPES``
 * (see agents/dark_pool.py). The hash of the type-encoding is what
 * ``ecrecover`` will compare against, so re-ordering breaks the signature.
 */
export const EIP3009_TRANSFER_TYPES = {
  TransferWithAuthorization: [
    { name: "from", type: "address" },
    { name: "to", type: "address" },
    { name: "value", type: "uint256" },
    { name: "validAfter", type: "uint256" },
    { name: "validBefore", type: "uint256" },
    { name: "nonce", type: "bytes32" },
  ],
} as const;

/** USDC EIP-712 domain on Arc testnet — confirmed by Slice 4 audit. */
export const USDC_ARC_DOMAIN_NAME = "USDC";
export const USDC_ARC_DOMAIN_VERSION = "2";

/** Circle Gateway batched scheme constants — match `@circle-fin/x402-batching`. */
export const GATEWAY_BATCHED_DOMAIN_NAME = "GatewayWalletBatched";
export const GATEWAY_BATCHED_DOMAIN_VERSION = "1";

/** Random 32-byte hex with ``0x`` prefix — used as EIP-3009 nonce. */
export function randomNonceHex(): Hex {
  // Use Web Crypto (Node 20+). Avoids pulling in a polyfill.
  const buf = new Uint8Array(32);
  globalThis.crypto.getRandomValues(buf);
  let hex = "0x";
  for (const b of buf) hex += b.toString(16).padStart(2, "0");
  return hex as Hex;
}

/** Pick the EIP-712 domain to sign against, based on a 402 ``accepts`` entry.
 *
 * Two cases:
 *   - direct USDC mode: ``extra.name === "USDC"`` (or unset → default USDC). Sign
 *     against ``verifyingContract = accepts.asset`` (the USDC token).
 *   - Gateway batched: ``extra.name === "GatewayWalletBatched"``. Sign against
 *     ``verifyingContract = extra.verifyingContract`` (the GatewayWallet).
 */
export interface ResolvedDomain {
  name: string;
  version: string;
  chainId: number;
  verifyingContract: Address;
  /** Mode hint for downstream logging / routing. */
  mode: "direct" | "gateway-batched";
}

export function resolveDomain(
  requirements: PaymentRequirements,
  chainId: number,
): ResolvedDomain {
  const extraName = requirements.extra?.name as string | undefined;
  const extraVersion = requirements.extra?.version as string | undefined;

  if (extraName === GATEWAY_BATCHED_DOMAIN_NAME) {
    const vc = requirements.extra?.verifyingContract as Address | undefined;
    if (!vc) {
      throw new SignerUnusableError(
        "server advertised Gateway batching but omitted extra.verifyingContract",
      );
    }
    return {
      name: GATEWAY_BATCHED_DOMAIN_NAME,
      version: extraVersion ?? GATEWAY_BATCHED_DOMAIN_VERSION,
      chainId,
      verifyingContract: vc,
      mode: "gateway-batched",
    };
  }

  // Default: direct USDC mode. Use the server's ``asset`` as verifyingContract.
  return {
    name: extraName ?? USDC_ARC_DOMAIN_NAME,
    version: extraVersion ?? USDC_ARC_DOMAIN_VERSION,
    chainId,
    verifyingContract: requirements.asset,
    mode: "direct",
  };
}

/**
 * Adapter wrapping a ``RawEoa`` into a viem ``PrivateKeyAccount`` if (and only
 * if) the EOA carries a private key. Turnkey-backed EOAs (privateKey absent)
 * need the Turnkey HSM to sign — that path lives in ``scripts/wallet/`` and is
 * out of scope for this slice. We surface a clean error in that case.
 */
export class TurnkeyEoaSigner {
  public readonly address: Address;
  public readonly backedByTEE: boolean;
  private readonly account: PrivateKeyAccount | null;

  constructor(eoa: RawEoa) {
    this.address = eoa.address;
    this.backedByTEE = eoa.backedByTEE;
    if (eoa.privateKey) {
      this.account = privateKeyToAccount(eoa.privateKey);
      if (this.account.address.toLowerCase() !== eoa.address.toLowerCase()) {
        throw new SignerUnusableError(
          `EOA address ${eoa.address} does not match private-key-derived address ` +
            `${this.account.address}. Refusing to sign.`,
        );
      }
    } else {
      this.account = null;
    }
  }

  /** Sign an EIP-712 typed-data payload. Returns the 65-byte r||s||v hex. */
  async signTypedData(params: {
    domain: {
      name: string;
      version: string;
      chainId: number;
      verifyingContract: Address;
    };
    types: Record<string, Array<{ name: string; type: string }>>;
    primaryType: string;
    message: Record<string, unknown>;
  }): Promise<Hex> {
    if (!this.account) {
      throw new SignerUnusableError(
        "EOA has no privateKey (Turnkey-backed). Use a Turnkey signer path " +
          "from scripts/wallet/ for real broadcast, or pass a local EOA " +
          "(forceLocal=true) for dry-run signing.",
      );
    }
    // viem's PrivateKeyAccount.signTypedData has the shape we need but with a
    // narrower ``types`` type — cast at the boundary because we accept the
    // generic shape from `@circle-fin/x402-batching`'s ``BatchEvmSigner``.
    return this.account.signTypedData({
      domain: params.domain,
      types: params.types,
      primaryType: params.primaryType,
      // viem's typing is precise; cast to satisfy the dynamic primaryType.
      message: params.message,
    } as Parameters<PrivateKeyAccount["signTypedData"]>[0]);
  }
}

/** Sign an EIP-3009 ``TransferWithAuthorization`` and return a ``PaymentAuthorization``.
 *
 * Phase 3 audit (F11) hardening:
 *   - ``expectedRecipient`` (optional): when set, the signer refuses to sign
 *     if ``requirements.payTo`` differs. Throws ``SignerUnusableError``.
 *     This is the TS analogue of Python's ``x402_pay_and_post(..., expected_recipient=...)``
 *     and prevents a malicious / compromised server from rewriting payTo
 *     after the price was pre-approved.
 *   - ``maxAmountBaseUnits`` (optional): a strict hard cap. When set, the
 *     signer rejects ANY ``requirements.maxAmountRequired`` or ``value``
 *     that exceeds this cap. Throws ``X402AmountExceededError``. This is
 *     stricter than ``X402BatchClient.pickAccept`` (which caps
 *     ``maxAmountRequired`` but trusts that the caller passes a clean
 *     ``value``) and defends the building block itself.
 */
export async function signTransferWithAuthorization(args: {
  signer: TurnkeyEoaSigner;
  requirements: PaymentRequirements;
  chainId: number;
  value: UsdcBaseUnits;
  validForSeconds: number;
  nonce?: Hex;
  /** Time source override (testing). */
  now?: () => number;
  /** Pin the server's recipient. Refuse to sign if requirements.payTo differs. */
  expectedRecipient?: Address;
  /** Strict cap on both requirements.maxAmountRequired and value. */
  maxAmountBaseUnits?: UsdcBaseUnits;
}): Promise<PaymentAuthorization> {
  const {
    signer,
    requirements,
    chainId,
    value,
    validForSeconds,
    nonce,
    now = () => Math.floor(Date.now() / 1000),
    expectedRecipient,
    maxAmountBaseUnits,
  } = args;

  // F11 — recipient pinning. Throws BEFORE we touch the signer / Turnkey HSM.
  if (
    expectedRecipient !== undefined &&
    requirements.payTo.toLowerCase() !== expectedRecipient.toLowerCase()
  ) {
    throw new SignerUnusableError(
      `server payTo ${requirements.payTo} does not match expected recipient ${expectedRecipient}`,
    );
  }

  // F11 — strict amount enforcement at the building-block layer. Both the
  // server's quoted maxAmountRequired AND the caller-supplied ``value``
  // must stay within the cap.
  if (maxAmountBaseUnits !== undefined) {
    const required = BigInt(requirements.maxAmountRequired);
    if (required > maxAmountBaseUnits) {
      throw new X402AmountExceededError(required, maxAmountBaseUnits);
    }
    if (value > maxAmountBaseUnits) {
      throw new X402AmountExceededError(value, maxAmountBaseUnits);
    }
  }

  const domain = resolveDomain(requirements, chainId);
  const nonceHex = nonce ?? randomNonceHex();
  const nowSec = now();
  // validAfter trails by 1s — matches agents/x402_client.py to give the
  // server's clock-skew buffer (`now+5`) some slack.
  const validAfter = BigInt(nowSec - 1);
  const validBefore = BigInt(nowSec + validForSeconds);

  const message: TransferWithAuthorization = {
    from: signer.address,
    to: requirements.payTo,
    value,
    validAfter,
    validBefore,
    nonce: nonceHex,
  };

  const signature = (await signer.signTypedData({
    domain: {
      name: domain.name,
      version: domain.version,
      chainId: domain.chainId,
      verifyingContract: domain.verifyingContract,
    },
    types: EIP3009_TRANSFER_TYPES as unknown as Record<
      string,
      Array<{ name: string; type: string }>
    >,
    primaryType: "TransferWithAuthorization",
    message: {
      from: message.from,
      to: message.to,
      value: message.value,
      validAfter: message.validAfter,
      validBefore: message.validBefore,
      nonce: message.nonce,
    },
  })) as Hex;

  return {
    authorization: message,
    signature,
    network: requirements.network,
    scheme: requirements.scheme,
    verifyingContract: domain.verifyingContract,
    chainId: domain.chainId,
    domainName: domain.name,
    domainVersion: domain.version,
    payTo: requirements.payTo,
    asset: requirements.asset,
    resource: requirements.resource,
    signedAt: nowSec,
  };
}

/**
 * Build the base64-encoded X-PAYMENT header value the dark pool expects.
 *
 * Wire shape (matches agents/dark_pool.py:build_signed_payment_header):
 *   {
 *     "x402Version": 1,
 *     "scheme": "exact",
 *     "network": "arc-testnet",
 *     "payload": {
 *       "signature": "0x...",
 *       "authorization": {
 *         "from": "0x...",
 *         "to":   "0x...",
 *         "value": "1000",
 *         "validAfter":  "1716...",
 *         "validBefore": "1716...",
 *         "nonce": "0x..."
 *       }
 *     }
 *   }
 *
 * The dark pool reads each numeric field as a string and casts to int.
 */
export function encodeXPaymentHeader(auth: PaymentAuthorization, x402Version = 1): string {
  const payload = {
    x402Version,
    scheme: auth.scheme,
    network: auth.network,
    payload: {
      signature: auth.signature,
      authorization: {
        from: auth.authorization.from,
        to: auth.authorization.to,
        value: auth.authorization.value.toString(),
        validAfter: auth.authorization.validAfter.toString(),
        validBefore: auth.authorization.validBefore.toString(),
        nonce: auth.authorization.nonce,
      },
    },
  };
  const json = JSON.stringify(payload);
  // base64 of UTF-8 bytes — matches Python's ``base64.b64encode(json.dumps(...).encode())``.
  return Buffer.from(json, "utf-8").toString("base64");
}
