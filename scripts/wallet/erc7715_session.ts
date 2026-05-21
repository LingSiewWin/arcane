/**
 * ERC-7715 session-key issuance with parent-bound sub-delegation.
 *
 * ERC-7715 is the "Wallet Permissions" RPC standard (issued via
 * `wallet_grantPermissions`). The SCA exposes a permission record that
 * authorizes a signer to act for it under bounded scope (target/selector
 * allowlist, value cap, expiry).
 *
 * Circle's deployed SCA implementation does NOT natively expose ERC-7715
 * at the time of writing. We follow the spec's documented fallback:
 *
 *   "If Circle's SCA doesn't natively support 7715 yet, use a simple
 *    setSpendingLimit(token, amount, expiry) admin call as a placeholder
 *    and document it."
 *
 * So this module:
 *   1. Encodes a `SessionKeyAuth` payload that captures the budget, expiry,
 *      scope, and constitutionHash binding.
 *   2. Encodes a fallback `setSpendingLimit(address token, uint256 amount,
 *      uint256 expiry, address sessionSigner, bytes32 scopeRoot)` calldata
 *      ready to be submitted to the SCA when the live path is enabled.
 *   3. Enforces the SubdelegationExceedsParentBounds invariant locally — the
 *      contract-level check (per Slice 2 / `ConstitutionHook.sol`) will be
 *      the authoritative gate when live.
 */

import {
  encodeAbiParameters,
  encodeFunctionData,
  keccak256,
  parseUnits,
  toHex,
  getAddress,
  type Hex,
} from "viem";
import type {
  Bytes32,
  CircleScaInfo,
  EthAddress,
  SessionKeyAuth,
  SessionScope,
  TurnkeyEoa,
} from "./types.js";
import {
  MissingEnvironment,
  SubdelegationExceedsParentBounds,
} from "./types.js";
import { pollCircleTx, readCircleEnv } from "./circle_sca.js";

/** USDC has 6 decimals on Arc (confirmed in use-arc.md). */
export const USDC_DECIMALS = 6;

/** USDC address on Arc testnet (confirmed in agent_arc_integration.md). */
export const USDC_ARC_TESTNET: EthAddress = getAddress(
  "0x3600000000000000000000000000000000000000",
);

/**
 * Compute a Merkle-style hash over the scope list. We use a deterministic
 * keccak of the alphabetically-sorted scopes; that gives a stable bytes32
 * "scopeRoot" we can plug into both the off-chain auth and the on-chain
 * setSpendingLimit fallback.
 */
function scopeRoot(scopes: SessionScope[]): Bytes32 {
  const sorted = [...scopes].sort();
  return keccak256(toHex(sorted.join(",")));
}

/**
 * Local enforcement of "sub-delegation cannot relax parent's bounds."
 * Mirrors the contract-level revert reason from Slice 2.
 *
 * Rules:
 *   - child.budget <= parent.budget
 *   - child.expiry <= parent.expiry
 *   - child.scopes ⊆ parent.scopes
 */
export function assertSubdelegationBounds(args: {
  parent: SessionKeyAuth;
  childBudgetBaseUnits: bigint;
  childExpiry: number;
  childScopes: SessionScope[];
}): void {
  const { parent, childBudgetBaseUnits, childExpiry, childScopes } = args;
  if (childBudgetBaseUnits > parent.budgetUSDCBaseUnits) {
    throw new SubdelegationExceedsParentBounds(
      `child budget ${childBudgetBaseUnits} > parent budget ${parent.budgetUSDCBaseUnits}`,
    );
  }
  if (childExpiry > parent.expiry) {
    throw new SubdelegationExceedsParentBounds(
      `child expiry ${childExpiry} > parent expiry ${parent.expiry}`,
    );
  }
  const parentScopeSet = new Set(parent.scopes);
  for (const s of childScopes) {
    if (!parentScopeSet.has(s)) {
      throw new SubdelegationExceedsParentBounds(
        `child scope "${s}" is not in parent scopes [${parent.scopes.join(", ")}]`,
      );
    }
  }
}

/**
 * Build the install-data hex blob that captures all session-key parameters
 * (signer, sca, token, budget, expiry, scopeRoot, constitutionHash). This is
 * the bytes the SCA receives + stores as the permission record. When the
 * live ERC-7715 path opens up, the same struct should map cleanly into the
 * `wallet_grantPermissions` payload.
 */
export function encodeSessionInstallData(args: {
  signer: EthAddress;
  sca: EthAddress;
  token: EthAddress;
  budgetBaseUnits: bigint;
  expiry: number;
  scopes: SessionScope[];
  constitutionHash: Bytes32;
}): Hex {
  return encodeAbiParameters(
    [
      { name: "signer", type: "address" },
      { name: "sca", type: "address" },
      { name: "token", type: "address" },
      { name: "budget", type: "uint256" },
      { name: "expiry", type: "uint256" },
      { name: "scopeRoot", type: "bytes32" },
      { name: "constitutionHash", type: "bytes32" },
    ],
    [
      args.signer,
      args.sca,
      args.token,
      args.budgetBaseUnits,
      BigInt(args.expiry),
      scopeRoot(args.scopes),
      args.constitutionHash,
    ],
  );
}

/**
 * Fallback calldata: `setSpendingLimit(address token, uint256 amount,
 * uint256 expiry, address sessionSigner, bytes32 scopeRoot)`.
 *
 * This is the admin path we submit until Circle exposes 7715 directly.
 */
const SET_SPENDING_LIMIT_ABI = [
  {
    name: "setSpendingLimit",
    type: "function",
    stateMutability: "nonpayable",
    inputs: [
      { name: "token", type: "address" },
      { name: "amount", type: "uint256" },
      { name: "expiry", type: "uint256" },
      { name: "sessionSigner", type: "address" },
      { name: "scopeRoot", type: "bytes32" },
    ],
    outputs: [],
  },
] as const;

export function encodeSetSpendingLimitCalldata(args: {
  token: EthAddress;
  amount: bigint;
  expiry: number;
  sessionSigner: EthAddress;
  scopes: SessionScope[];
}): Hex {
  return encodeFunctionData({
    abi: SET_SPENDING_LIMIT_ABI,
    functionName: "setSpendingLimit",
    args: [
      args.token,
      args.amount,
      BigInt(args.expiry),
      args.sessionSigner,
      scopeRoot(args.scopes),
    ],
  });
}

/**
 * Issue a session key on the SCA, binding it to a Turnkey EOA signer.
 *
 * Dry-run: returns an unbroadcast SessionKeyAuth (txHash undefined). All
 * payload bytes are encoded — the caller can inspect them, hand them to
 * Slice 2 contracts for unit tests, or replay them later.
 *
 * Live: submits the fallback setSpendingLimit admin call via Circle's
 * createContractExecutionTransaction.
 */
export async function issueSessionKey(args: {
  signer: TurnkeyEoa;
  sca: CircleScaInfo;
  budgetUSDC: number;
  expiryMinutes: number;
  scopes: SessionScope[];
  constitutionHash: Bytes32;
  parent?: SessionKeyAuth;
  dryRun: boolean;
}): Promise<SessionKeyAuth> {
  const {
    signer,
    sca,
    budgetUSDC,
    expiryMinutes,
    scopes,
    constitutionHash,
    parent,
    dryRun,
  } = args;

  if (budgetUSDC < 0) {
    throw new Error(`budgetUSDC must be non-negative, got ${budgetUSDC}`);
  }
  if (expiryMinutes <= 0) {
    throw new Error(`expiryMinutes must be > 0, got ${expiryMinutes}`);
  }
  if (scopes.length === 0) {
    throw new Error("scopes must contain at least one SessionScope");
  }

  const budgetBaseUnits = parseUnits(budgetUSDC.toString(), USDC_DECIMALS);
  const expiry = Math.floor(Date.now() / 1000) + expiryMinutes * 60;

  if (parent) {
    assertSubdelegationBounds({
      parent,
      childBudgetBaseUnits: budgetBaseUnits,
      childExpiry: expiry,
      childScopes: scopes,
    });
  }

  const installData = encodeSessionInstallData({
    signer: signer.address,
    sca: sca.address,
    token: USDC_ARC_TESTNET,
    budgetBaseUnits,
    expiry,
    scopes,
    constitutionHash,
  });

  const auth: SessionKeyAuth = {
    signer: signer.address,
    sca: sca.address,
    budgetUSDCBaseUnits: budgetBaseUnits,
    expiry,
    scopes,
    constitutionHash,
    installData,
    ...(parent && {
      parent: {
        signer: parent.signer,
        budgetUSDCBaseUnits: parent.budgetUSDCBaseUnits,
        expiry: parent.expiry,
        scopes: parent.scopes,
      },
    }),
  };

  if (dryRun) {
    return auth;
  }

  // Live path: submit the fallback setSpendingLimit admin call against the SCA.
  const env = readCircleEnv();
  if (!env) {
    throw new MissingEnvironment(
      "CIRCLE_API_KEY / CIRCLE_ENTITY_SECRET (required for --dry-run=false)",
    );
  }
  const sdk = (await import(
    "@circle-fin/developer-controlled-wallets"
  )) as unknown as {
    initiateDeveloperControlledWalletsClient: (opts: {
      apiKey: string;
      entitySecret: string;
    }) => unknown;
  };
  const client = sdk.initiateDeveloperControlledWalletsClient({
    apiKey: env.apiKey,
    entitySecret: env.entitySecret,
  }) as unknown as {
    createContractExecutionTransaction(input: {
      walletAddress: string;
      blockchain: string;
      contractAddress: string;
      abiFunctionSignature: string;
      abiParameters: unknown[];
      fee: { type: "level"; config: { feeLevel: "LOW" | "MEDIUM" | "HIGH" } };
    }): Promise<{ data?: { id?: string } }>;
    getTransaction(input: { id: string }): Promise<{
      data?: { transaction?: { state?: string; txHash?: string } };
    }>;
  };
  const tx = await client.createContractExecutionTransaction({
    walletAddress: sca.address,
    blockchain: "ARC-TESTNET",
    contractAddress: sca.address,
    abiFunctionSignature:
      "setSpendingLimit(address,uint256,uint256,address,bytes32)",
    abiParameters: [
      USDC_ARC_TESTNET,
      budgetBaseUnits.toString(),
      expiry.toString(),
      signer.address,
      scopeRoot(scopes),
    ],
    fee: { type: "level", config: { feeLevel: "MEDIUM" } },
  });
  const id = tx.data?.id;
  if (!id) {
    throw new Error("setSpendingLimit tx submission returned no id");
  }
  const txHash = await pollCircleTx(client, id);
  return { ...auth, installTxHash: txHash };
}
