/**
 * ERC-8004 canonical identity mint.
 *
 * Source of truth: https://github.com/erc-8004/erc-8004-contracts
 *   IdentityRegistryUpgradeable.sol (read 2026-05-22):
 *
 *     struct MetadataEntry {
 *         string metadataKey;
 *         bytes  metadataValue;
 *     }
 *     function register(
 *         string memory agentURI,
 *         MetadataEntry[] memory metadata
 *     ) external returns (uint256 agentId);
 *
 *     function setAgentWallet(
 *         uint256 agentId,
 *         address newWallet,
 *         uint256 deadline,
 *         bytes calldata signature
 *     ) external;
 *
 *   Wallet-binding signature is EIP-712 over:
 *     AgentWalletSet(uint256 agentId, address newWallet, address owner, uint256 deadline)
 *   Domain:  name="ERC8004IdentityRegistry", version="1",
 *            chainId=<chain>, verifyingContract=<registry>
 *   Deadline window: must be within MAX_DEADLINE_DELAY = 5 minutes of now.
 *
 *   On token transfer the registry auto-clears "agentWallet" via _update().
 *
 * Arc Testnet identity registry: 0x8004A818BFB912233c491871b3d84c89A494BD9e.
 *
 * The OLD code in this file assumed `register(string)` (one of three Canonical
 * overloads). That worked, but it skipped:
 *   - structured metadata (constitutionHash, dark_pool_endpoint),
 *   - the agentWallet binding step (so ERC-8004-aware indexers cannot
 *     auto-discover the Turnkey EOA).
 * Phase 5 mandate: use the canonical metadata + wallet-binding path so the
 * identity layer is interoperable with any ERC-8004 indexer.
 */

import {
  createPublicClient,
  decodeEventLog,
  defineChain,
  encodeAbiParameters,
  getAddress,
  http,
  keccak256,
  parseAbiItem,
  parseAbi,
  toBytes,
  toHex,
  stringToHex,
  type Hex,
} from "viem";
import { privateKeyToAccount } from "viem/accounts";
import type {
  Bytes32,
  CircleScaInfo,
  EthAddress,
  TurnkeyEoa,
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

/** EIP-712 domain for the canonical ERC-8004 IdentityRegistry. */
export const ERC8004_EIP712_DOMAIN_NAME = "ERC8004IdentityRegistry";
export const ERC8004_EIP712_DOMAIN_VERSION = "1";

/** Maximum future window the registry accepts for the wallet-binding deadline. */
export const ERC8004_MAX_DEADLINE_DELAY_SECONDS = 5 * 60;

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
 * Canonical ABI we hit on IdentityRegistry. Restricted to the methods + events
 * `mintIdentity` actually uses — anyone integrating further is expected to read
 * the registry source upstream.
 */
export const IDENTITY_REGISTRY_ABI = parseAbi([
  // The overloaded register form. struct MetadataEntry = (string,bytes).
  "function register(string agentURI, (string metadataKey,bytes metadataValue)[] metadata) external returns (uint256 agentId)",
  // The agentWallet binder.
  "function setAgentWallet(uint256 agentId, address newWallet, uint256 deadline, bytes signature) external",
  // Views we depend on.
  "function getAgentWallet(uint256 agentId) external view returns (address)",
  "function ownerOf(uint256 agentId) external view returns (address)",
  // The canonical Registered event (signature: 0x...). We listen for this in
  // resolveIdentityIdFromLogs.
  "event Registered(uint256 indexed agentId, string agentURI, address indexed owner)",
] as const);

/**
 * Default agent-metadata URI for the demo. Slice 1/4 can override per agent.
 * (This is the URI Circle's own ERC-8004 tutorial uses.)
 */
export const DEFAULT_METADATA_URI =
  "ipfs://bafkreibdi6623n3xpf7ymk62ckb4bo75o3qemwkpfvp5i25j66itxvsoei";

/** Canonical metadata keys we set on registration. */
export const METADATA_KEY_CONSTITUTION_HASH = "constitutionHash";
export const METADATA_KEY_DARK_POOL_ENDPOINT = "dark_pool_endpoint";
export const METADATA_KEY_AGENT_NAME = "agentName";

/**
 * A MetadataEntry to be set at register-time. The canonical struct in
 * IdentityRegistryUpgradeable.sol is `(string metadataKey, bytes metadataValue)`.
 * The reserved key "agentWallet" is NOT settable via this array — the registry
 * sets it automatically on register() and clears it on transfer.
 */
export type MetadataEntry = {
  metadataKey: string;
  metadataValue: Hex;
};

/**
 * Build the metadata array we attach to every ERC-8004 registration. Carries:
 *   - "constitutionHash": the bytes32 constitution hash bound to this agent.
 *   - "dark_pool_endpoint": optional URL string for the x402 dark-pool resource.
 *   - "agentName": human-readable agent name.
 *
 * Reviewers asked for these specific keys so external indexers (and our own
 * orchestrator) can recover the constitution + endpoint without parsing the
 * metadata URI.
 */
export function buildMetadataEntries(args: {
  constitutionHash: Bytes32;
  darkPoolEndpoint?: string;
  agentName?: string;
}): MetadataEntry[] {
  const entries: MetadataEntry[] = [
    {
      metadataKey: METADATA_KEY_CONSTITUTION_HASH,
      // bytes32 → bytes (32 bytes wide).
      metadataValue: args.constitutionHash,
    },
  ];
  if (args.darkPoolEndpoint !== undefined) {
    entries.push({
      metadataKey: METADATA_KEY_DARK_POOL_ENDPOINT,
      metadataValue: stringToHex(args.darkPoolEndpoint),
    });
  }
  if (args.agentName !== undefined) {
    entries.push({
      metadataKey: METADATA_KEY_AGENT_NAME,
      metadataValue: stringToHex(args.agentName),
    });
  }
  return entries;
}

/**
 * Compute the EIP-712 typed-data digest the registry expects for
 * setAgentWallet. Exported for tests; production callers should use
 * `signAgentWalletBinding` which wraps this.
 */
export function buildAgentWalletSetDigest(args: {
  agentId: bigint;
  newWallet: EthAddress;
  owner: EthAddress;
  deadline: bigint;
  chainId: number;
  verifyingContract: EthAddress;
}): Hex {
  const { agentId, newWallet, owner, deadline, chainId, verifyingContract } = args;

  // typeHash = keccak256("AgentWalletSet(uint256 agentId,address newWallet,address owner,uint256 deadline)")
  const TYPE_HASH = keccak256(
    toBytes(
      "AgentWalletSet(uint256 agentId,address newWallet,address owner,uint256 deadline)",
    ),
  );

  // structHash = keccak256(abi.encode(TYPE_HASH, agentId, newWallet, owner, deadline))
  const structHash = keccak256(
    encodeAbiParameters(
      [
        { type: "bytes32" },
        { type: "uint256" },
        { type: "address" },
        { type: "address" },
        { type: "uint256" },
      ],
      [TYPE_HASH, agentId, newWallet, owner, deadline],
    ),
  );

  // EIP-712 domain separator for ("ERC8004IdentityRegistry", "1", chainId, registry).
  // DOMAIN_TYPEHASH = keccak256("EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)")
  const DOMAIN_TYPE_HASH = keccak256(
    toBytes(
      "EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)",
    ),
  );
  const nameHash = keccak256(toBytes(ERC8004_EIP712_DOMAIN_NAME));
  const versionHash = keccak256(toBytes(ERC8004_EIP712_DOMAIN_VERSION));
  const domainSeparator = keccak256(
    encodeAbiParameters(
      [
        { type: "bytes32" },
        { type: "bytes32" },
        { type: "bytes32" },
        { type: "uint256" },
        { type: "address" },
      ],
      [DOMAIN_TYPE_HASH, nameHash, versionHash, BigInt(chainId), verifyingContract],
    ),
  );

  // digest = keccak256("\x19\x01" || domainSeparator || structHash)
  const prefix = "0x1901" as Hex;
  return keccak256(
    `${prefix}${domainSeparator.slice(2)}${structHash.slice(2)}` as Hex,
  );
}

/**
 * Sign the EIP-712 typed-data binding the Turnkey EOA to the identity NFT.
 *
 * Two paths:
 *   1. Local-fallback EOA (privateKey present): sign locally via viem.
 *   2. Turnkey-backed EOA: requires Turnkey signRawPayload — NOT implemented
 *      in this slice (acknowledged in progress.md). We throw rather than ship
 *      a stub that silently mis-signs.
 */
export async function signAgentWalletBinding(args: {
  signer: TurnkeyEoa;
  agentId: bigint;
  owner: EthAddress;
  deadline: bigint;
  chainId: number;
  verifyingContract: EthAddress;
}): Promise<Hex> {
  const { signer, agentId, owner, deadline, chainId, verifyingContract } = args;
  const digest = buildAgentWalletSetDigest({
    agentId,
    newWallet: signer.address,
    owner,
    deadline,
    chainId,
    verifyingContract,
  });

  if (signer.privateKey !== undefined) {
    // Local fallback path (test / dry-run only). We sign the digest directly:
    // the registry calls `ECDSA.tryRecover(digest, signature)` on this exact
    // 32-byte digest (no further \x19 wrapping).
    const account = privateKeyToAccount(signer.privateKey);
    return account.sign({ hash: digest });
  }

  // Turnkey path — must call signRawPayload(payload=digest, encoding=HEX,
  // hashFunction=NO_OP) to get the registry-compatible signature. Stubbing this
  // would invent a fake signature; refuse loudly instead.
  throw new MissingEnvironment(
    "Turnkey signRawPayload integration for setAgentWallet — not implemented in this slice. " +
      "Either pass --dry-run or supply a TurnkeyEoa with a local privateKey for the wallet-binding step.",
  );
}

/**
 * Mint an ERC-8004 identity for the SCA. Returns:
 *   - identityId: decimal-string token id
 *   - registerTxHash: hash of the register tx (if broadcast)
 *   - setWalletTxHash: hash of the setAgentWallet tx (if broadcast)
 *
 * Pipeline:
 *   1. Encode `register(agentURI, MetadataEntry[])` via canonical ABI.
 *   2. Submit through Circle's createContractExecutionTransaction. Poll.
 *   3. Resolve the agentId from the canonical `Registered` event.
 *   4. If a Turnkey EOA was supplied, build the EIP-712 binding signature and
 *      call `setAgentWallet(agentId, eoa, deadline, sig)` to bind the EOA.
 *
 * Dry-run: returns a deterministic stub id derived from the SCA address +
 * constitutionHash so tests are stable. No RPC call.
 */
export async function mintIdentity(args: {
  sca: CircleScaInfo;
  constitutionHash: Bytes32;
  metadataURI: string;
  dryRun: boolean;
  /** Optional. When supplied AND dryRun=false, we'll bind this EOA via setAgentWallet. */
  turnkeyEoa?: TurnkeyEoa;
  /** Optional. URL of the dark-pool endpoint to advertise in metadata. */
  darkPoolEndpoint?: string;
  /** Optional. Agent name to record in metadata. */
  agentName?: string;
  /** Override chain id (default: arcTestnet.id). Used in tests that don't hit Arc. */
  chainId?: number;
}): Promise<{
  identityId: string;
  registerTxHash?: Hex;
  setWalletTxHash?: Hex;
  metadataEntries: MetadataEntry[];
}> {
  const {
    sca,
    constitutionHash,
    metadataURI,
    dryRun,
    turnkeyEoa,
    darkPoolEndpoint,
    agentName,
  } = args;
  const chainId = args.chainId ?? arcTestnet.id;

  const metadataEntries = buildMetadataEntries({
    constitutionHash,
    ...(darkPoolEndpoint !== undefined && { darkPoolEndpoint }),
    ...(agentName !== undefined && { agentName }),
  });

  if (dryRun) {
    // Deterministic stub: take low 8 hex chars of (sca XOR constitutionHash) as
    // a uint id. Same inputs -> same id, different inputs -> different id.
    const a = BigInt(sca.address);
    const b = BigInt(constitutionHash);
    // eslint-disable-next-line @typescript-eslint/no-magic-numbers
    const stub = (a ^ b) % 1_000_000_000n;
    return { identityId: stub.toString(), metadataEntries };
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

  // Canonical register(string,(string,bytes)[]) signature.
  // Circle's `abiParameters` accepts ABI-encoded tuples as nested arrays.
  const metadataParam = metadataEntries.map((m) => [m.metadataKey, m.metadataValue]);
  const registerTx = await client.createContractExecutionTransaction({
    walletAddress: sca.address,
    blockchain: "ARC-TESTNET",
    contractAddress: IDENTITY_REGISTRY,
    abiFunctionSignature: "register(string,(string,bytes)[])",
    abiParameters: [metadataURI, metadataParam],
    fee: { type: "level", config: { feeLevel: "MEDIUM" } },
  });
  const registerId = registerTx.data?.id;
  if (!registerId) {
    throw new IdentityMintFailed(
      "createContractExecutionTransaction for register(string,(string,bytes)[]) returned no id",
    );
  }
  const registerTxHash = await pollCircleTx(client, registerId);

  // Resolve agentId from canonical `Registered(uint256,string,address)` event.
  const identityIdStr = await resolveIdentityIdFromLogs({
    sca: sca.address,
    rpcUrl: arcTestnet.rpcUrls.default.http[0]!,
  });

  // Step 4: bind the EOA via setAgentWallet, if one was supplied.
  let setWalletTxHash: Hex | undefined;
  if (turnkeyEoa !== undefined) {
    const deadline = BigInt(
      Math.floor(Date.now() / 1000) + Math.floor(ERC8004_MAX_DEADLINE_DELAY_SECONDS / 2),
    );
    const sig = await signAgentWalletBinding({
      signer: turnkeyEoa,
      agentId: BigInt(identityIdStr),
      owner: sca.address,
      deadline,
      chainId,
      verifyingContract: IDENTITY_REGISTRY,
    });
    const bindTx = await client.createContractExecutionTransaction({
      walletAddress: sca.address,
      blockchain: "ARC-TESTNET",
      contractAddress: IDENTITY_REGISTRY,
      abiFunctionSignature: "setAgentWallet(uint256,address,uint256,bytes)",
      abiParameters: [
        identityIdStr,
        turnkeyEoa.address,
        deadline.toString(),
        sig,
      ],
      fee: { type: "level", config: { feeLevel: "MEDIUM" } },
    });
    const bindId = bindTx.data?.id;
    if (!bindId) {
      throw new IdentityMintFailed(
        "createContractExecutionTransaction for setAgentWallet returned no id",
      );
    }
    setWalletTxHash = await pollCircleTx(client, bindId);
  }

  return {
    identityId: identityIdStr,
    registerTxHash,
    ...(setWalletTxHash !== undefined && { setWalletTxHash }),
    metadataEntries,
  };
}

/**
 * Query the IdentityRegistry's canonical `Registered(uint256,string,address)`
 * event log filtered to the SCA owner and return the latest agentId.
 *
 * We deliberately listen for `Registered` (the spec event) rather than the
 * generic ERC-721 `Transfer(0x0,owner,tokenId)`, because the spec event
 * carries the agentURI we set — useful for downstream verification.
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

  // Canonical spec event from IdentityRegistryUpgradeable.sol.
  const registeredEvent = parseAbiItem(
    "event Registered(uint256 indexed agentId, string agentURI, address indexed owner)",
  );
  const logs = await publicClient.getLogs({
    address: IDENTITY_REGISTRY,
    event: registeredEvent,
    args: { owner: sca },
    fromBlock,
    toBlock: latest,
  });
  const last = logs[logs.length - 1];
  if (!last || last.args.agentId === undefined) {
    // Fallback: try the legacy ERC-721 Transfer event. This keeps us compatible
    // with the pre-canonical deployment we'd been pointing at, and means the
    // function won't regress if the Arc deployment is an early fork.
    const transferLogs = await publicClient.getLogs({
      address: IDENTITY_REGISTRY,
      event: parseAbiItem(
        "event Transfer(address indexed from, address indexed to, uint256 indexed tokenId)",
      ),
      args: { to: sca },
      fromBlock,
      toBlock: latest,
    });
    const lastT = transferLogs[transferLogs.length - 1];
    if (!lastT || lastT.args.tokenId === undefined) {
      throw new IdentityMintFailed(
        `No Registered or Transfer events to ${sca} on IdentityRegistry within last ${blockRange} blocks`,
      );
    }
    return lastT.args.tokenId.toString();
  }
  return last.args.agentId.toString();
}

/**
 * Helper: decode a `Registered` event log against the canonical ABI. Surfaces
 * the (agentId, agentURI, owner) tuple. Used by tests + scripts that watch the
 * registry for new mints.
 */
export function decodeRegisteredEvent(log: {
  data: Hex;
  topics: [Hex, ...Hex[]];
}): { agentId: bigint; agentURI: string; owner: EthAddress } {
  const decoded = decodeEventLog({
    abi: IDENTITY_REGISTRY_ABI,
    eventName: "Registered",
    data: log.data,
    topics: log.topics,
  });
  return {
    agentId: decoded.args.agentId,
    agentURI: decoded.args.agentURI,
    owner: decoded.args.owner,
  };
}

/**
 * Tiny helper used by tests to construct a viem-friendly hex representation
 * of `MetadataEntry[]` for assertion.
 */
export function encodeMetadataEntriesAsHex(entries: MetadataEntry[]): Hex {
  return encodeAbiParameters(
    [{ type: "tuple[]", components: [{ type: "string" }, { type: "bytes" }] }],
    [entries.map((e) => [e.metadataKey, e.metadataValue]) as readonly [string, Hex][]],
  );
}

// re-exports used by tests
export { toHex };
