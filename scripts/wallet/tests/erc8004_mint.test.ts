/**
 * Vitest suite for the canonical ERC-8004 mint path.
 *
 * Source of truth: https://github.com/erc-8004/erc-8004-contracts
 * (IdentityRegistryUpgradeable.sol). All assertions in this file are
 * cross-checked against that contract — when the canonical interface changes,
 * update this file FIRST, then propagate to erc8004_mint.ts.
 */

import { describe, expect, it } from "vitest";
import {
  decodeAbiParameters,
  encodeAbiParameters,
  getAddress,
  hexToString,
  keccak256,
  recoverAddress,
  stringToHex,
  toBytes,
  toHex,
  type Hex,
} from "viem";
import { privateKeyToAccount, generatePrivateKey } from "viem/accounts";
import {
  buildAgentWalletSetDigest,
  buildMetadataEntries,
  DEFAULT_METADATA_URI,
  encodeMetadataEntriesAsHex,
  ERC8004_EIP712_DOMAIN_NAME,
  ERC8004_EIP712_DOMAIN_VERSION,
  ERC8004_MAX_DEADLINE_DELAY_SECONDS,
  IDENTITY_REGISTRY,
  METADATA_KEY_AGENT_NAME,
  METADATA_KEY_CONSTITUTION_HASH,
  METADATA_KEY_DARK_POOL_ENDPOINT,
  mintIdentity,
  signAgentWalletBinding,
  arcTestnet,
} from "../erc8004_mint.js";
import {
  demoConstitutionHash,
} from "../spawn_agent.js";
import type { Bytes32, TurnkeyEoa, CircleScaInfo } from "../types.js";

const BOB_CONSTITUTION = demoConstitutionHash("bob-no-leverage-above-2x");

function localEoa(): TurnkeyEoa {
  const pk = generatePrivateKey();
  const account = privateKeyToAccount(pk);
  return {
    address: account.address,
    privateKey: pk,
    backedByTEE: false,
  };
}

function localSca(owner: TurnkeyEoa, salt = "test-sca"): CircleScaInfo {
  // Deterministic-ish stand-in: hash owner + salt and take low 40 hex.
  const digest = keccak256(toBytes(`${owner.address}|${salt}`));
  return {
    address: getAddress(`0x${digest.slice(-40)}`),
    deployed: false,
    owner: owner.address,
  };
}

describe("buildMetadataEntries (canonical ERC-8004 keys)", () => {
  it("always includes constitutionHash as a 32-byte value", () => {
    const entries = buildMetadataEntries({
      constitutionHash: BOB_CONSTITUTION,
    });
    expect(entries).toHaveLength(1);
    expect(entries[0]!.metadataKey).toBe(METADATA_KEY_CONSTITUTION_HASH);
    expect(entries[0]!.metadataValue).toBe(BOB_CONSTITUTION);
    // bytes32 → 0x + 64 hex chars
    expect(entries[0]!.metadataValue).toMatch(/^0x[0-9a-fA-F]{64}$/);
  });

  it("includes dark_pool_endpoint when supplied", () => {
    const endpoint = "https://alice.darkpool.example/query";
    const entries = buildMetadataEntries({
      constitutionHash: BOB_CONSTITUTION,
      darkPoolEndpoint: endpoint,
    });
    const dpEntry = entries.find(
      (e) => e.metadataKey === METADATA_KEY_DARK_POOL_ENDPOINT,
    );
    expect(dpEntry).toBeDefined();
    expect(hexToString(dpEntry!.metadataValue)).toBe(endpoint);
  });

  it("includes agentName when supplied", () => {
    const entries = buildMetadataEntries({
      constitutionHash: BOB_CONSTITUTION,
      agentName: "bob",
    });
    const nameEntry = entries.find(
      (e) => e.metadataKey === METADATA_KEY_AGENT_NAME,
    );
    expect(nameEntry).toBeDefined();
    expect(hexToString(nameEntry!.metadataValue)).toBe("bob");
  });

  it("never sets the reserved 'agentWallet' key", () => {
    const entries = buildMetadataEntries({
      constitutionHash: BOB_CONSTITUTION,
      darkPoolEndpoint: "https://x.example",
      agentName: "z",
    });
    for (const e of entries) {
      expect(e.metadataKey).not.toBe("agentWallet");
    }
  });

  it("encodes MetadataEntry[] as the canonical tuple[] ABI shape", () => {
    const entries = buildMetadataEntries({
      constitutionHash: BOB_CONSTITUTION,
      darkPoolEndpoint: "https://e",
    });
    const encoded = encodeMetadataEntriesAsHex(entries);
    // Round-trip: decoding the encoded bytes must yield the same entries.
    const [decoded] = decodeAbiParameters(
      [{ type: "tuple[]", components: [{ type: "string" }, { type: "bytes" }] }],
      encoded,
    ) as unknown as [Array<[string, Hex]>];
    expect(decoded.length).toBe(entries.length);
    for (let i = 0; i < entries.length; i++) {
      const row = decoded[i]!;
      expect(row[0]).toBe(entries[i]!.metadataKey);
      expect(row[1].toLowerCase()).toBe(
        entries[i]!.metadataValue.toLowerCase(),
      );
    }
  });
});

describe("buildAgentWalletSetDigest (EIP-712 over canonical type string)", () => {
  it("matches the manual EIP-712 hash on canonical AgentWalletSet inputs", () => {
    const agentId = 42n;
    const newWallet = "0x000000000000000000000000000000000000beef" as `0x${string}`;
    const owner = "0x000000000000000000000000000000000000cafe" as `0x${string}`;
    const deadline = 1_800_000_000n;
    const chainId = 5042002;
    const verifyingContract = IDENTITY_REGISTRY;

    const got = buildAgentWalletSetDigest({
      agentId,
      newWallet,
      owner,
      deadline,
      chainId,
      verifyingContract,
    });

    // Independent manual recompute (mirrors the contract's exact construction).
    const TYPE_HASH = keccak256(
      toBytes(
        "AgentWalletSet(uint256 agentId,address newWallet,address owner,uint256 deadline)",
      ),
    );
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
    const DOMAIN_TYPE_HASH = keccak256(
      toBytes(
        "EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)",
      ),
    );
    const domainSep = keccak256(
      encodeAbiParameters(
        [
          { type: "bytes32" },
          { type: "bytes32" },
          { type: "bytes32" },
          { type: "uint256" },
          { type: "address" },
        ],
        [
          DOMAIN_TYPE_HASH,
          keccak256(toBytes(ERC8004_EIP712_DOMAIN_NAME)),
          keccak256(toBytes(ERC8004_EIP712_DOMAIN_VERSION)),
          BigInt(chainId),
          verifyingContract,
        ],
      ),
    );
    const expected = keccak256(
      `0x1901${domainSep.slice(2)}${structHash.slice(2)}` as Hex,
    );
    expect(got.toLowerCase()).toBe(expected.toLowerCase());
  });

  it("differs when any field of the typed struct differs", () => {
    const base = {
      agentId: 1n,
      newWallet: "0x000000000000000000000000000000000000beef" as `0x${string}`,
      owner: "0x000000000000000000000000000000000000cafe" as `0x${string}`,
      deadline: 1_700_000_000n,
      chainId: 5042002,
      verifyingContract: IDENTITY_REGISTRY,
    };
    const d0 = buildAgentWalletSetDigest(base);
    const d1 = buildAgentWalletSetDigest({ ...base, agentId: 2n });
    const d2 = buildAgentWalletSetDigest({ ...base, deadline: 1_700_000_001n });
    const d3 = buildAgentWalletSetDigest({ ...base, chainId: 1 });
    expect(d1).not.toBe(d0);
    expect(d2).not.toBe(d0);
    expect(d3).not.toBe(d0);
  });
});

describe("signAgentWalletBinding (local EOA path)", () => {
  it("produces a signature that recovers to the wallet being bound", async () => {
    const eoa = localEoa();
    const owner =
      "0x000000000000000000000000000000000000cafe" as `0x${string}`;
    const agentId = 99n;
    const deadline = BigInt(Math.floor(Date.now() / 1000) + 60);
    const chainId = arcTestnet.id;

    const sig = await signAgentWalletBinding({
      signer: eoa,
      agentId,
      owner,
      deadline,
      chainId,
      verifyingContract: IDENTITY_REGISTRY,
    });

    const digest = buildAgentWalletSetDigest({
      agentId,
      newWallet: eoa.address,
      owner,
      deadline,
      chainId,
      verifyingContract: IDENTITY_REGISTRY,
    });

    const recovered = await recoverAddress({
      hash: digest,
      signature: sig,
    });
    expect(recovered.toLowerCase()).toBe(eoa.address.toLowerCase());
  });

  it("refuses to fabricate a signature for a TEE-backed EOA without a Turnkey signer", async () => {
    const tee: TurnkeyEoa = {
      address: "0x000000000000000000000000000000000000feed" as `0x${string}`,
      turnkeyWalletId: "wallet-id-stub",
      backedByTEE: true,
    };
    await expect(
      signAgentWalletBinding({
        signer: tee,
        agentId: 1n,
        owner:
          "0x000000000000000000000000000000000000cafe" as `0x${string}`,
        deadline: BigInt(Math.floor(Date.now() / 1000) + 60),
        chainId: arcTestnet.id,
        verifyingContract: IDENTITY_REGISTRY,
      }),
    ).rejects.toThrow(/Turnkey signRawPayload/);
  });
});

describe("mintIdentity (dry-run, canonical metadata return)", () => {
  it("returns deterministic identityId + the metadata entries we will register", async () => {
    const eoa = localEoa();
    const sca = localSca(eoa);
    const result = await mintIdentity({
      sca,
      constitutionHash: BOB_CONSTITUTION,
      metadataURI: DEFAULT_METADATA_URI,
      dryRun: true,
      turnkeyEoa: eoa,
      darkPoolEndpoint: "https://alice.example/query",
      agentName: "alice",
    });
    expect(result.identityId).toMatch(/^\d+$/);

    // The metadata entries we'd send to register(string,(string,bytes)[]).
    const keys = result.metadataEntries.map((m) => m.metadataKey);
    expect(keys).toContain(METADATA_KEY_CONSTITUTION_HASH);
    expect(keys).toContain(METADATA_KEY_DARK_POOL_ENDPOINT);
    expect(keys).toContain(METADATA_KEY_AGENT_NAME);

    // constitutionHash entry must be the bytes32 verbatim.
    const cEntry = result.metadataEntries.find(
      (m) => m.metadataKey === METADATA_KEY_CONSTITUTION_HASH,
    )!;
    expect(cEntry.metadataValue).toBe(BOB_CONSTITUTION);
  });

  it("is deterministic across runs for the same SCA + constitutionHash", async () => {
    const eoa = localEoa();
    const sca = localSca(eoa, "deterministic");
    const r1 = await mintIdentity({
      sca,
      constitutionHash: BOB_CONSTITUTION,
      metadataURI: DEFAULT_METADATA_URI,
      dryRun: true,
    });
    const r2 = await mintIdentity({
      sca,
      constitutionHash: BOB_CONSTITUTION,
      metadataURI: DEFAULT_METADATA_URI,
      dryRun: true,
    });
    expect(r1.identityId).toBe(r2.identityId);
  });

  it("never tries to bind the reserved agentWallet metadata via the array", async () => {
    const eoa = localEoa();
    const sca = localSca(eoa);
    const result = await mintIdentity({
      sca,
      constitutionHash: BOB_CONSTITUTION,
      metadataURI: DEFAULT_METADATA_URI,
      dryRun: true,
      turnkeyEoa: eoa,
      agentName: "agent",
    });
    // The canonical registry rejects the reserved key in the metadata array
    // (it auto-sets agentWallet on register). We must never put it there.
    for (const m of result.metadataEntries) {
      expect(m.metadataKey).not.toBe("agentWallet");
    }
  });
});

describe("ERC8004_MAX_DEADLINE_DELAY_SECONDS sanity", () => {
  it("matches the 5-minute upstream constant", () => {
    expect(ERC8004_MAX_DEADLINE_DELAY_SECONDS).toBe(5 * 60);
  });
});

// Quiet TS unused-import warnings:
void stringToHex;
void toHex;
