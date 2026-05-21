/**
 * Circle SCA wrapping for an EOA.
 *
 * Architectural note (resolved via docs/agent_arc_integration.md + ERC-8004 tutorial):
 *
 *   The Arc samples (arc-escrow, arc-commerce, ...) use Circle
 *   Developer-Controlled Wallets with `accountType: "SCA"`. This produces a
 *   Circle-managed ERC-4337 smart contract account that is lazily deployed on
 *   first outbound user-op. The SCA is the entity that holds USDC, owns
 *   ERC-8004 identity, and (in our slice) mounts the ConstitutionHook validator
 *   module from Slice 2.
 *
 *   The Modular-Wallets SDK (`use-modular-wallets.md`) is the other path. It
 *   gives finer module control (ERC-6900 / ERC-7579 modules) but is wired for
 *   passkey-authenticated front-ends rather than the daemon spawner we need.
 *
 *   This slice therefore uses Developer-Controlled Wallets as the SCA source
 *   of truth, mirroring `arc-escrow/lib/utils/executeContract.ts` patterns.
 *   Module installation (ConstitutionHook) is exposed via the same
 *   `createContractExecutionTransaction` surface — we encode an
 *   `installModule(uint256,address,bytes)` call against the SCA address.
 *
 * Open question:
 *   The exact `installModule` ABI on Circle's SCA implementation is not in the
 *   skill docs. We use the canonical ERC-7579 signature:
 *     `installModule(uint256 moduleType, address module, bytes initData)`
 *   If the deployed Circle SCA implementation deviates, only this calldata
 *   encoding needs to change.
 */

import { keccak256, toHex, encodeFunctionData, getAddress } from "viem";
import type { Hex } from "viem";
import type {
  CircleScaInfo,
  EthAddress,
  Bytes32,
  TurnkeyEoa,
} from "./types.js";
import { MissingEnvironment } from "./types.js";

/**
 * Module type IDs from ERC-7579. We only care about the validator module type
 * (1) for the ConstitutionHook from Slice 2.
 */
export const ERC7579_MODULE_TYPE = {
  VALIDATOR: 1n,
  EXECUTOR: 2n,
  FALLBACK: 3n,
  HOOK: 4n,
} as const;

/** Standard ERC-7579 `installModule` ABI fragment. */
const INSTALL_MODULE_ABI = [
  {
    name: "installModule",
    type: "function",
    stateMutability: "nonpayable",
    inputs: [
      { name: "moduleTypeId", type: "uint256" },
      { name: "module", type: "address" },
      { name: "initData", type: "bytes" },
    ],
    outputs: [],
  },
] as const;

/**
 * Constitution hook module address. In production this is read from
 * Slice 2's deploy artifact. For now we read from env, with a clearly-marked
 * placeholder when the env is unset.
 */
function readConstitutionHookAddress(): EthAddress {
  const v = process.env.CONSTITUTION_HOOK_ADDRESS;
  if (v && /^0x[0-9a-fA-F]{40}$/.test(v)) {
    return getAddress(v);
  }
  // Placeholder address that is deterministic + recognisable (`0xC07577…`,
  // mnemonic "constitution"). Slice 2 must overwrite CONSTITUTION_HOOK_ADDRESS
  // before any real broadcast.
  return getAddress("0xC0775770000000000000000000000000C0DEC0DE");
}

/**
 * Deterministically derive a stable SCA address from an EOA owner + a salt.
 * This is used in --dry-run mode so tests are stable across runs. It is NOT a
 * real CREATE2 computation — we just hash the inputs so the value is unique
 * and reproducible.
 */
function deriveDryRunScaAddress(owner: EthAddress, salt: Hex): EthAddress {
  const digest = keccak256(
    toHex(`dry-run-sca|${owner.toLowerCase()}|${salt.toLowerCase()}`),
  );
  return getAddress(`0x${digest.slice(-40)}`);
}

/**
 * Build the calldata that, when sent to an SCA, installs the
 * ConstitutionHook validator module with the given constitutionHash as init
 * data. This is the bytes the agent commits to.
 */
export function encodeConstitutionInstallData(
  constitutionHash: Bytes32,
): { calldata: Hex; module: EthAddress; constitutionHash: Bytes32 } {
  const module = readConstitutionHookAddress();
  const calldata = encodeFunctionData({
    abi: INSTALL_MODULE_ABI,
    functionName: "installModule",
    args: [ERC7579_MODULE_TYPE.VALIDATOR, module, constitutionHash],
  });
  return { calldata, module, constitutionHash };
}

/** Env-var presence helper for Circle. */
export function readCircleEnv(): {
  apiKey: string;
  entitySecret: string;
} | null {
  const apiKey = process.env.CIRCLE_API_KEY;
  const entitySecret = process.env.CIRCLE_ENTITY_SECRET;
  if (!apiKey || !entitySecret) {
    return null;
  }
  return { apiKey, entitySecret };
}

/** Lazy-load the Circle SDK so dry-run / test paths don't require the dep at runtime. */
async function loadCircleSdk(): Promise<{
  initiateDeveloperControlledWalletsClient: (opts: {
    apiKey: string;
    entitySecret: string;
  }) => unknown;
}> {
  const mod = (await import(
    "@circle-fin/developer-controlled-wallets"
  )) as unknown as {
    initiateDeveloperControlledWalletsClient: (opts: {
      apiKey: string;
      entitySecret: string;
    }) => unknown;
  };
  return mod;
}

/**
 * Wrap an EOA in a Circle Developer-Controlled SCA on Arc Testnet.
 *
 * In `dryRun=true` mode, returns a deterministic placeholder SCA address
 * derived from the EOA. The SCA is marked `deployed=false`.
 *
 * In `dryRun=false` mode, calls Circle's createWallets API with
 * `accountType: "SCA"` on `ARC-TESTNET`. The SCA is lazily deployed by Circle
 * on first outbound user-op (per `use-developer-controlled-wallets.md`), so
 * we mark `deployed=false` here too — actual deployment happens during the
 * first contract execution (the ERC-8004 mint, in our case).
 */
export async function wrapEoaInCircleSca(args: {
  owner: TurnkeyEoa;
  name: string;
  dryRun: boolean;
}): Promise<CircleScaInfo> {
  const { owner, name, dryRun } = args;
  if (dryRun) {
    const salt = keccak256(toHex(`sca|${name}`));
    return {
      address: deriveDryRunScaAddress(owner.address, salt),
      deployed: false,
      owner: owner.address,
    };
  }
  const env = readCircleEnv();
  if (!env) {
    throw new MissingEnvironment(
      "CIRCLE_API_KEY / CIRCLE_ENTITY_SECRET (required for --dry-run=false)",
    );
  }
  const sdk = await loadCircleSdk();
  const client = sdk.initiateDeveloperControlledWalletsClient({
    apiKey: env.apiKey,
    entitySecret: env.entitySecret,
  }) as unknown as {
    createWalletSet(input: { name: string }): Promise<{
      data?: { walletSet?: { id: string } };
    }>;
    createWallets(input: {
      blockchains: string[];
      count: number;
      walletSetId: string;
      accountType: "SCA" | "EOA";
    }): Promise<{ data?: { wallets?: Array<{ address: string }> } }>;
  };
  // One wallet-set per agent name keeps things searchable in the Circle Console.
  const set = await client.createWalletSet({
    name: `agorahack-${name}-${Date.now()}`,
  });
  const walletSetId = set.data?.walletSet?.id;
  if (!walletSetId) {
    throw new Error("Circle createWalletSet did not return a wallet-set id");
  }
  const wallets = await client.createWallets({
    blockchains: ["ARC-TESTNET"],
    count: 1,
    walletSetId,
    accountType: "SCA",
  });
  const address = wallets.data?.wallets?.[0]?.address;
  if (!address || !/^0x[0-9a-fA-F]{40}$/.test(address)) {
    throw new Error(
      `Circle createWallets returned malformed SCA address: ${String(address)}`,
    );
  }
  return {
    address: getAddress(address),
    deployed: false, // lazy deployment on first user-op
    owner: owner.address,
  };
}

/**
 * Install the ConstitutionHook on an existing SCA. In --dry-run, returns
 * `undefined` for the tx hash. In real mode, submits an
 * `installModule(VALIDATOR, hook, constitutionHash)` call via Circle's
 * `createContractExecutionTransaction`.
 *
 * Note: Circle's `abiFunctionSignature` + `abiParameters` interface needs the
 * signature string + parameters as strings. We pass `constitutionHash` as the
 * hex-encoded bytes32 string. The Circle SDK handles the rest.
 */
export async function installConstitutionHook(args: {
  sca: CircleScaInfo;
  constitutionHash: Bytes32;
  dryRun: boolean;
}): Promise<{ installTxHash?: Hex; installData: Hex; module: EthAddress }> {
  const { sca, constitutionHash, dryRun } = args;
  const { calldata, module } = encodeConstitutionInstallData(constitutionHash);
  if (dryRun) {
    return { installData: calldata, module };
  }
  const env = readCircleEnv();
  if (!env) {
    throw new MissingEnvironment(
      "CIRCLE_API_KEY / CIRCLE_ENTITY_SECRET (required for --dry-run=false)",
    );
  }
  const sdk = await loadCircleSdk();
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
  // The SCA installs the module on itself, so walletAddress === contractAddress.
  const tx = await client.createContractExecutionTransaction({
    walletAddress: sca.address,
    blockchain: "ARC-TESTNET",
    contractAddress: sca.address,
    abiFunctionSignature: "installModule(uint256,address,bytes)",
    abiParameters: [
      ERC7579_MODULE_TYPE.VALIDATOR.toString(),
      module,
      constitutionHash,
    ],
    fee: { type: "level", config: { feeLevel: "MEDIUM" } },
  });
  const id = tx.data?.id;
  if (!id) {
    throw new Error("Circle createContractExecutionTransaction returned no id");
  }
  const txHash = await pollCircleTx(client, id);
  return { installTxHash: txHash, installData: calldata, module };
}

/**
 * Poll a Circle transaction id until it reaches a terminal state. Returns the
 * on-chain txHash on success.
 */
export async function pollCircleTx(
  client: {
    getTransaction(input: { id: string }): Promise<{
      data?: { transaction?: { state?: string; txHash?: string } };
    }>;
  },
  id: string,
  opts: { timeoutMs?: number; intervalMs?: number } = {},
): Promise<Hex> {
  const timeoutMs = opts.timeoutMs ?? 60_000;
  const intervalMs = opts.intervalMs ?? 2_000;
  const start = Date.now();
  while (Date.now() - start < timeoutMs) {
    await new Promise((r) => setTimeout(r, intervalMs));
    const { data } = await client.getTransaction({ id });
    const state = data?.transaction?.state;
    if (state === "COMPLETE") {
      const hash = data?.transaction?.txHash;
      if (!hash || !hash.startsWith("0x")) {
        throw new Error(
          `Circle tx ${id} COMPLETE but no txHash returned`,
        );
      }
      return hash as Hex;
    }
    if (state === "FAILED" || state === "DENIED" || state === "CANCELLED") {
      throw new Error(`Circle tx ${id} ended in terminal state ${state}`);
    }
  }
  throw new Error(`Circle tx ${id} polling timed out after ${timeoutMs}ms`);
}
