/**
 * ERC-8004 identity mint against the Arc-deployed IdentityRegistry.
 *
 * Source of truth: Arc tutorial `register-your-first-ai-agent.md`
 *   IdentityRegistry on Arc Testnet: 0x8004A818BFB912233c491871b3d84c89A494BD9e
 *   Function:                        register(string metadataURI)
 *   Side effect:                     ERC-721 Transfer(0x0, owner, tokenId)
 *
 * The design spec referenced `register(address controller, bytes32 metadataHash)`
 * but the actual deployed contract takes a single `string metadataURI`. We
 * follow the on-chain truth.
 */

import {
  createPublicClient,
  defineChain,
  getAddress,
  http,
  parseAbiItem,
  type Hex,
} from "viem";
import type {
  Bytes32,
  CircleScaInfo,
  EthAddress,
} from "./types.js";
import {
  IdentityMintFailed,
  MissingEnvironment,
} from "./types.js";
import { pollCircleTx, readCircleEnv } from "./circle_sca.js";

/** Arc Testnet identity registry — verified live (see docs/agent_arc_integration.md). */
export const IDENTITY_REGISTRY: EthAddress = getAddress(
  "0x8004A818BFB912233c491871b3d84c89A494BD9e",
);

/** Arc Testnet chain definition. We define inline so we don't depend on a specific viem version's chain catalog. */
export const arcTestnet = defineChain({
  id: 5042002,
  name: "Arc Testnet",
  nativeCurrency: { name: "USDC", symbol: "USDC", decimals: 18 },
  rpcUrls: {
    default: {
      http: [process.env.RPC ?? "https://rpc.testnet.arc.network"],
    },
  },
  blockExplorers: {
    default: { name: "Arcscan", url: "https://testnet.arcscan.app" },
  },
});

/**
 * Default agent-metadata URI for the demo. Slice 1/4 can override per agent.
 * (This is the URI Circle's own ERC-8004 tutorial uses.)
 */
export const DEFAULT_METADATA_URI =
  "ipfs://bafkreibdi6623n3xpf7ymk62ckb4bo75o3qemwkpfvp5i25j66itxvsoei";

/**
 * Mint an ERC-8004 identity for the SCA. Returns the token id (as decimal
 * string) and the mint tx hash.
 *
 * Dry-run: returns a deterministic stub id derived from the SCA address +
 * constitution hash so tests are stable. No RPC call.
 */
export async function mintIdentity(args: {
  sca: CircleScaInfo;
  constitutionHash: Bytes32;
  metadataURI: string;
  dryRun: boolean;
}): Promise<{ identityId: string; txHash?: Hex }> {
  const { sca, constitutionHash, metadataURI, dryRun } = args;
  if (dryRun) {
    // Deterministic stub: take low 8 hex chars of (sca XOR constitutionHash) as
    // a uint id. Same inputs -> same id, different inputs -> different id.
    const a = BigInt(sca.address);
    const b = BigInt(constitutionHash);
    // eslint-disable-next-line @typescript-eslint/no-magic-numbers
    const stub = (a ^ b) % 1_000_000_000n;
    return { identityId: stub.toString() };
  }

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
    contractAddress: IDENTITY_REGISTRY,
    abiFunctionSignature: "register(string)",
    abiParameters: [metadataURI],
    fee: { type: "level", config: { feeLevel: "MEDIUM" } },
  });
  const id = tx.data?.id;
  if (!id) {
    throw new IdentityMintFailed(
      "createContractExecutionTransaction returned no id",
    );
  }
  const txHash = await pollCircleTx(client, id);
  // Resolve token id from the Transfer(0x0, sca, tokenId) event log on the registry.
  const identityId = await resolveIdentityIdFromLogs({
    sca: sca.address,
    rpcUrl: arcTestnet.rpcUrls.default.http[0]!,
  });
  return { identityId, txHash };
}

/**
 * Query the IdentityRegistry's Transfer event log filtered to the SCA address
 * and return the latest token id. Throws `IdentityMintFailed` if no event was
 * emitted within the recent block window.
 */
export async function resolveIdentityIdFromLogs(args: {
  sca: EthAddress;
  rpcUrl: string;
  blockRange?: bigint;
}): Promise<string> {
  const { sca, rpcUrl } = args;
  const blockRange = args.blockRange ?? 10_000n;
  const publicClient = createPublicClient({
    chain: arcTestnet,
    transport: http(rpcUrl),
  });
  const latest = await publicClient.getBlockNumber();
  const fromBlock = latest > blockRange ? latest - blockRange : 0n;
  const logs = await publicClient.getLogs({
    address: IDENTITY_REGISTRY,
    event: parseAbiItem(
      "event Transfer(address indexed from, address indexed to, uint256 indexed tokenId)",
    ),
    args: { to: sca },
    fromBlock,
    toBlock: latest,
  });
  const last = logs[logs.length - 1];
  if (!last || last.args.tokenId === undefined) {
    throw new IdentityMintFailed(
      `No Transfer events to ${sca} on IdentityRegistry within last ${blockRange} blocks`,
    );
  }
  return last.args.tokenId.toString();
}
