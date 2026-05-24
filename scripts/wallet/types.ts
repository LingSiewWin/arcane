/**
 * Shared types for the Wallet Stack slice (Slice 3 of Constrained Cognition).
 *
 * Stack:
 *   Human -> Passkey -> Turnkey EOA (TEE) -> Circle SCA (ERC-7579)
 *                                              | ConstitutionHook installed (constitutionHash)
 *                                              | ERC-8004 identity bound
 *                                              | ERC-7715 session key issued
 *
 * The Turnkey EOA is the raw signer used for x402 (Circle Gateway requires ecrecover,
 * which is incompatible with ERC-1271). The Circle SCA is the funded smart account
 * that holds USDC, mounts the ConstitutionHook validator module, and issues
 * scoped session keys to the EOA.
 */

import type { Hex, Address } from "viem";

/** Hex address shape, kept as a viem-native type alias. */
export type EthAddress = Address;

/** Hex string (used for hashes, transactions, calldata). */
export type HexString = Hex;

/** A keccak256-shaped 32-byte hash. */
export type Bytes32 = `0x${string}`;

/**
 * Scope categories that a session key may target. Demo-scoped enum: real
 * ERC-7715 supports much richer policies (per-selector, per-target ACLs).
 */
export type SessionScope =
  | "x402_pay"          // Sign EIP-3009 / EIP-712 typed data for Circle Gateway nanopayments.
  | "trade_execute"     // Submit user-ops that hit the constitution-hooked trade entry point.
  | "memory_anchor"     // Call MemoryAnchor.anchor(bytes32) on Arc.
  | "subagent_spawn";   // Allowed to spawn child agents (sub-delegation root).

/**
 * The on-chain-ish authorization payload returned by ERC-7715 issuance.
 *
 * Note: when ERC-7715 is not natively supported by the underlying SCA
 * implementation (Circle Modular Wallets do not yet expose 7715 directly),
 * the same payload is materialized via an admin `setSpendingLimit` style
 * call on the SCA. See `erc7715_session.ts` for the fallback path.
 */
export type SessionKeyAuth = {
  /** The session signer (a Turnkey EOA, in our case). */
  signer: EthAddress;
  /** The SCA the session key is delegated to operate on. */
  sca: EthAddress;
  /** USDC budget the session can spend across its lifetime, in 6-decimal base units. */
  budgetUSDCBaseUnits: bigint;
  /** UNIX timestamp (seconds) at which the session key expires. */
  expiry: number;
  /** Allowed scopes for this session. */
  scopes: SessionScope[];
  /** Optional parent session this delegation derives from (for sub-delegation). */
  parent?: {
    signer: EthAddress;
    budgetUSDCBaseUnits: bigint;
    expiry: number;
    scopes: SessionScope[];
  };
  /** ConstitutionHook hash bound to this session (so child agents inherit). */
  constitutionHash: Bytes32;
  /** Hex calldata-ready encoding of the permission as it would be installed on the SCA. */
  installData: HexString;
  /** Tx hash from the SCA call that committed the permission (undefined in dry-run). */
  installTxHash?: HexString;
};

/**
 * Result of `spawnAgent({ ... })`. This is the load-bearing interface contract
 * shared with Slice 1 (agents/) and Slice 4 (demo_e2e.py).
 */
export type AgentSpawnResult = {
  /** Smart account address (ERC-4337 / ERC-7579 modular SCA wrapped around the Turnkey EOA). */
  scaAddress: EthAddress;
  /** ERC-8004 IdentityRegistry token id (NFT id), serialized as decimal string. */
  identityId: string;
  /** Issued session key authorization. */
  sessionKey: SessionKeyAuth;
  /** Raw Turnkey EOA address — this signs x402 payloads via ecrecover. */
  turnkeyEoa: EthAddress;
  /**
   * Canonical ERC-8004 MetadataEntry[] we attached at register-time. Keys we
   * always set: "constitutionHash" (bytes32) and optionally "dark_pool_endpoint"
   * (utf8 url bytes) + "agentName" (utf8 bytes). Shape matches the upstream
   * `IdentityRegistryUpgradeable.register(string, (string,bytes)[])` ABI.
   */
  metadataEntries: Array<{ metadataKey: string; metadataValue: HexString }>;
  /** Transaction hashes for each step. Undefined when the step was dry-run-skipped. */
  txHashes: {
    identityMint?: HexString;
    /** Canonical ERC-8004 setAgentWallet binding tx (binds the Turnkey EOA to the agentId). */
    identitySetAgentWallet?: HexString;
    scaDeploy?: HexString;
    sessionKeyIssue?: HexString;
  };
};

/** Input shape for `spawnAgent`. */
export type SpawnAgentInput = {
  name: string;
  budget_USDC: number;
  expiryMinutes: number;
  constitutionHash: Bytes32;
  parentSessionKey?: SessionKeyAuth;
  /** When true (default), no RPC calls are made; addresses are deterministic from `name`. */
  dryRun?: boolean;
  /** Override the scope set for the session key. Defaults to `["x402_pay", "trade_execute", "memory_anchor"]`. */
  scopes?: SessionScope[];
  /** Optional metadata URI for ERC-8004; defaults to the canonical demo placeholder. */
  metadataURI?: string;
  /**
   * Optional dark-pool URL to advertise in ERC-8004 metadata under the
   * "dark_pool_endpoint" key. ERC-8004-aware indexers can discover this agent's
   * x402-paywalled query endpoint without parsing the agentURI JSON.
   */
  darkPoolEndpoint?: string;
};

/** A Turnkey-or-local EOA descriptor. */
export type TurnkeyEoa = {
  address: EthAddress;
  /** Private key. ONLY populated for the local random fallback (test mode). Never logged. */
  privateKey?: HexString;
  /** Turnkey wallet id, when backed by a real Turnkey account. */
  turnkeyWalletId?: string;
  /** Whether the key lives in a Turnkey TEE (true) or was generated locally (false). */
  backedByTEE: boolean;
};

/** Result of a Circle SCA deployment/derivation. */
export type CircleScaInfo = {
  address: EthAddress;
  /** Whether the SCA was already counterfactually deployed (lazy deployment is OK). */
  deployed: boolean;
  /** The deploy tx hash, if a real broadcast happened this run. */
  deployTxHash?: HexString;
  /** The owner EOA address. */
  owner: EthAddress;
};

/**
 * Custom error thrown when a sub-delegation tries to exceed bounds of its parent.
 * Contract surface: `SubdelegationExceedsParentBounds`.
 */
export class SubdelegationExceedsParentBounds extends Error {
  public readonly code = "SubdelegationExceedsParentBounds" as const;
  constructor(reason: string) {
    super(`SubdelegationExceedsParentBounds: ${reason}`);
    this.name = "SubdelegationExceedsParentBounds";
  }
}

/** Custom error thrown when ERC-8004 mint cannot be confirmed (no Transfer event). */
export class IdentityMintFailed extends Error {
  public readonly code = "IdentityMintFailed" as const;
  constructor(reason: string) {
    super(`IdentityMintFailed: ${reason}`);
    this.name = "IdentityMintFailed";
  }
}

/** Custom error thrown when an env config required for a live broadcast is missing. */
export class MissingEnvironment extends Error {
  public readonly code = "MissingEnvironment" as const;
  constructor(variable: string) {
    super(`MissingEnvironment: required env var ${variable} is not set`);
    this.name = "MissingEnvironment";
  }
}
