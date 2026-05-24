/**
 * Minimal ABI fragments — only the functions this dashboard actually reads.
 * Extracted from contracts/out/<Name>.sol/<Name>.json (`abi` field) and the
 * Pyth IPyth interface. Kept narrow on purpose: read-only view calls.
 */

/** ERC-8004 IdentityRegistry / ERC-721 ownerOf. */
export const identityRegistryAbi = [
  {
    type: "function",
    name: "ownerOf",
    stateMutability: "view",
    inputs: [{ name: "tokenId", type: "uint256" }],
    outputs: [{ name: "", type: "address" }],
  },
] as const;

/** MemoryAnchor — history length + anchor lookup. */
export const memoryAnchorAbi = [
  {
    type: "function",
    name: "historyLength",
    stateMutability: "view",
    inputs: [{ name: "identityId", type: "uint256" }],
    outputs: [{ name: "", type: "uint256" }],
  },
  {
    type: "function",
    name: "anchorAt",
    stateMutability: "view",
    inputs: [
      { name: "identityId", type: "uint256" },
      { name: "sequence", type: "uint64" },
    ],
    outputs: [
      { name: "root", type: "bytes32" },
      { name: "ownerAtAnchor", type: "address" },
      { name: "timestamp", type: "uint256" },
    ],
  },
] as const;

/** BondVault — agent bond balance. */
export const bondVaultAbi = [
  {
    type: "function",
    name: "balanceOf",
    stateMutability: "view",
    inputs: [{ name: "agent", type: "address" }],
    outputs: [{ name: "", type: "uint256" }],
  },
] as const;

/**
 * AgentRegistry — the arena backbone. Directory views, the register write,
 * and the AgentAction / AgentRegistered events the live feed decodes.
 * Extracted verbatim from contracts/out/AgentRegistry.sol/AgentRegistry.json.
 */
export const agentRegistryAbi = [
  {
    type: "function",
    name: "agentCount",
    stateMutability: "view",
    inputs: [],
    outputs: [{ name: "", type: "uint256" }],
  },
  {
    type: "function",
    name: "agentByIdentity",
    stateMutability: "view",
    inputs: [{ name: "", type: "uint256" }],
    outputs: [{ name: "", type: "uint256" }],
  },
  {
    type: "function",
    name: "getAgent",
    stateMutability: "view",
    inputs: [{ name: "agentId", type: "uint256" }],
    outputs: [
      {
        name: "",
        type: "tuple",
        components: [
          { name: "identityId", type: "uint256" },
          { name: "constitutionHash", type: "bytes32" },
          { name: "bondVault", type: "address" },
          { name: "darkPoolUrl", type: "string" },
          { name: "operator", type: "address" },
          { name: "registeredAt", type: "uint64" },
          { name: "active", type: "bool" },
        ],
      },
    ],
  },
  {
    type: "function",
    name: "register",
    stateMutability: "nonpayable",
    inputs: [
      { name: "identityId", type: "uint256" },
      { name: "constitutionHash", type: "bytes32" },
      { name: "bondVault", type: "address" },
      { name: "darkPoolUrl", type: "string" },
    ],
    outputs: [{ name: "agentId", type: "uint256" }],
  },
  {
    type: "event",
    name: "AgentRegistered",
    inputs: [
      { name: "agentId", type: "uint256", indexed: true },
      { name: "identityId", type: "uint256", indexed: true },
      { name: "operator", type: "address", indexed: true },
      { name: "constitutionHash", type: "bytes32", indexed: false },
    ],
    anonymous: false,
  },
  {
    type: "event",
    name: "AgentAction",
    inputs: [
      { name: "agentId", type: "uint256", indexed: true },
      { name: "kind", type: "uint8", indexed: true },
      { name: "payload", type: "bytes", indexed: false },
      { name: "timestamp", type: "uint256", indexed: false },
    ],
    anonymous: false,
  },
] as const;

/**
 * PerformanceOracle — the reputation / leaderboard source. We only decode the
 * AdviceResolved event (slashed=loss, !slashed=win) to derive win/loss records.
 */
export const performanceOracleAbi = [
  {
    type: "event",
    name: "AdviceResolved",
    inputs: [
      { name: "agent", type: "address", indexed: true },
      { name: "p0", type: "int64", indexed: false },
      { name: "p1", type: "int64", indexed: false },
      { name: "rBps", type: "int256", indexed: false },
      { name: "slashed", type: "bool", indexed: false },
    ],
    anonymous: false,
  },
] as const;

/** Pyth — getPriceUnsafe returns the latest cached price tuple. */
export const pythAbi = [
  {
    type: "function",
    name: "getPriceUnsafe",
    stateMutability: "view",
    inputs: [{ name: "id", type: "bytes32" }],
    outputs: [
      {
        name: "price",
        type: "tuple",
        components: [
          { name: "price", type: "int64" },
          { name: "conf", type: "uint64" },
          { name: "expo", type: "int32" },
          { name: "publishTime", type: "uint256" },
        ],
      },
    ],
  },
] as const;
