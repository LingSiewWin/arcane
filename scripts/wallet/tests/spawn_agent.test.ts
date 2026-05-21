/**
 * Vitest suite for spawn_agent. All tests run in dry-run mode — no RPC calls,
 * no Turnkey API, no Circle API. Real-network smoke tests live behind a
 * separate `pnpm run test:e2e` (not in this slice).
 */

import { describe, expect, it } from "vitest";
import { keccak256, parseUnits, toHex } from "viem";
import {
  demoConstitutionHash,
  spawnAgent,
} from "../spawn_agent.js";
import {
  assertSubdelegationBounds,
  encodeSessionInstallData,
  USDC_ARC_TESTNET,
} from "../erc7715_session.js";
import { encodeConstitutionInstallData } from "../circle_sca.js";
import {
  SubdelegationExceedsParentBounds,
  type SessionKeyAuth,
} from "../types.js";

const BOB_CONSTITUTION = demoConstitutionHash("bob-no-leverage-above-2x");

describe("spawnAgent (dry-run)", () => {
  it("returns a plausible AgentSpawnResult without RPC calls", async () => {
    const result = await spawnAgent({
      name: "bob",
      budget_USDC: 10,
      expiryMinutes: 5,
      constitutionHash: BOB_CONSTITUTION,
      dryRun: true,
    });

    // Addresses must be 0x-prefixed 40-hex.
    expect(result.scaAddress).toMatch(/^0x[0-9a-fA-F]{40}$/);
    expect(result.turnkeyEoa).toMatch(/^0x[0-9a-fA-F]{40}$/);

    // SCA and EOA are different entities.
    expect(result.scaAddress.toLowerCase()).not.toEqual(
      result.turnkeyEoa.toLowerCase(),
    );

    // Identity id is a decimal string.
    expect(result.identityId).toMatch(/^\d+$/);

    // Session key bound to the SCA + EOA we returned.
    expect(result.sessionKey.signer.toLowerCase()).toEqual(
      result.turnkeyEoa.toLowerCase(),
    );
    expect(result.sessionKey.sca.toLowerCase()).toEqual(
      result.scaAddress.toLowerCase(),
    );

    // Budget is encoded in 6-decimal USDC base units.
    expect(result.sessionKey.budgetUSDCBaseUnits).toEqual(
      parseUnits("10", 6),
    );

    // Expiry is roughly now + 5 minutes (allow 30s clock drift).
    const expectedExpiry = Math.floor(Date.now() / 1000) + 5 * 60;
    expect(
      Math.abs(result.sessionKey.expiry - expectedExpiry),
    ).toBeLessThan(30);

    // ConstitutionHash propagated.
    expect(result.sessionKey.constitutionHash).toEqual(BOB_CONSTITUTION);

    // Dry-run skips broadcasts, so no tx hashes recorded.
    expect(result.txHashes.identityMint).toBeUndefined();
    expect(result.txHashes.scaDeploy).toBeUndefined();
    expect(result.txHashes.sessionKeyIssue).toBeUndefined();
  });

  it("encodes the constitutionHash into the SCA install data", async () => {
    const result = await spawnAgent({
      name: "alice",
      budget_USDC: 1,
      expiryMinutes: 5,
      constitutionHash: BOB_CONSTITUTION,
      dryRun: true,
    });

    // Independently encode and check the validator-install calldata.
    const { calldata } = encodeConstitutionInstallData(BOB_CONSTITUTION);

    // Standard ERC-7579 installModule selector = first 4 bytes of
    // keccak256("installModule(uint256,address,bytes)").
    const selector = "0x9517e29f";
    expect(calldata.startsWith(selector)).toBe(true);

    // The constitutionHash must appear as 32-byte word within the calldata.
    const hashWord = BOB_CONSTITUTION.slice(2).toLowerCase();
    expect(calldata.toLowerCase()).toContain(hashWord);

    // And it must equal the constitutionHash recorded on the session-key auth.
    expect(result.sessionKey.constitutionHash).toEqual(BOB_CONSTITUTION);

    // The session-key install data must ALSO carry the constitutionHash word.
    expect(result.sessionKey.installData.toLowerCase()).toContain(hashWord);
  });

  it("sub-delegation within parent bounds succeeds", async () => {
    const parent = await spawnAgent({
      name: "bob",
      budget_USDC: 10,
      expiryMinutes: 60,
      constitutionHash: BOB_CONSTITUTION,
      dryRun: true,
    });

    const child = await spawnAgent({
      name: "bob-child",
      budget_USDC: 5,
      expiryMinutes: 5,
      constitutionHash: BOB_CONSTITUTION,
      parentSessionKey: parent.sessionKey,
      dryRun: true,
    });

    expect(child.sessionKey.budgetUSDCBaseUnits).toEqual(parseUnits("5", 6));
    expect(child.sessionKey.parent).toBeDefined();
    expect(child.sessionKey.parent?.signer.toLowerCase()).toEqual(
      parent.sessionKey.signer.toLowerCase(),
    );
    expect(child.sessionKey.parent?.budgetUSDCBaseUnits).toEqual(
      parent.sessionKey.budgetUSDCBaseUnits,
    );
  });

  it("sub-delegation exceeding parent's budget throws SubdelegationExceedsParentBounds", async () => {
    const parent = await spawnAgent({
      name: "bob",
      budget_USDC: 10,
      expiryMinutes: 60,
      constitutionHash: BOB_CONSTITUTION,
      dryRun: true,
    });

    await expect(
      spawnAgent({
        name: "bob-child-greedy",
        budget_USDC: 50, // > parent's 10
        expiryMinutes: 5,
        constitutionHash: BOB_CONSTITUTION,
        parentSessionKey: parent.sessionKey,
        dryRun: true,
      }),
    ).rejects.toThrow(SubdelegationExceedsParentBounds);
  });

  it("sub-delegation exceeding parent's expiry throws", async () => {
    const parent = await spawnAgent({
      name: "bob",
      budget_USDC: 10,
      expiryMinutes: 5, // very short window
      constitutionHash: BOB_CONSTITUTION,
      dryRun: true,
    });

    await expect(
      spawnAgent({
        name: "bob-child-long",
        budget_USDC: 1,
        expiryMinutes: 60, // > parent's 5 minutes
        constitutionHash: BOB_CONSTITUTION,
        parentSessionKey: parent.sessionKey,
        dryRun: true,
      }),
    ).rejects.toThrow(SubdelegationExceedsParentBounds);
  });

  it("sub-delegation with scope outside parent's scopes throws", async () => {
    const parent = await spawnAgent({
      name: "bob",
      budget_USDC: 10,
      expiryMinutes: 60,
      constitutionHash: BOB_CONSTITUTION,
      scopes: ["x402_pay"], // narrow parent
      dryRun: true,
    });

    await expect(
      spawnAgent({
        name: "bob-child-overbroad",
        budget_USDC: 1,
        expiryMinutes: 5,
        constitutionHash: BOB_CONSTITUTION,
        scopes: ["x402_pay", "trade_execute"], // trade_execute not in parent
        parentSessionKey: parent.sessionKey,
        dryRun: true,
      }),
    ).rejects.toThrow(SubdelegationExceedsParentBounds);
  });

  it("rejects malformed constitutionHash", async () => {
    await expect(
      spawnAgent({
        name: "bob",
        budget_USDC: 1,
        expiryMinutes: 5,
        constitutionHash: "0xnotahex" as `0x${string}`,
        dryRun: true,
      }),
    ).rejects.toThrow(/constitutionHash/);
  });
});

describe("assertSubdelegationBounds (unit)", () => {
  const parent: SessionKeyAuth = {
    signer: "0x000000000000000000000000000000000000beef" as `0x${string}`,
    sca: "0x000000000000000000000000000000000000cafe" as `0x${string}`,
    budgetUSDCBaseUnits: parseUnits("10", 6),
    expiry: 2_000_000_000,
    scopes: ["x402_pay", "trade_execute"],
    constitutionHash: BOB_CONSTITUTION,
    installData: "0x" as `0x${string}`,
  };

  it("accepts a strict subset", () => {
    expect(() =>
      assertSubdelegationBounds({
        parent,
        childBudgetBaseUnits: parseUnits("5", 6),
        childExpiry: 1_999_999_999,
        childScopes: ["x402_pay"],
      }),
    ).not.toThrow();
  });

  it("rejects equal budget but expanded scope", () => {
    expect(() =>
      assertSubdelegationBounds({
        parent,
        childBudgetBaseUnits: parseUnits("10", 6),
        childExpiry: 1_999_999_999,
        childScopes: ["x402_pay", "memory_anchor"], // memory_anchor not in parent
      }),
    ).toThrow(SubdelegationExceedsParentBounds);
  });
});

describe("encodeSessionInstallData (unit)", () => {
  it("is deterministic across runs with the same inputs", () => {
    const a = encodeSessionInstallData({
      signer: "0x000000000000000000000000000000000000beef" as `0x${string}`,
      sca: "0x000000000000000000000000000000000000cafe" as `0x${string}`,
      token: USDC_ARC_TESTNET,
      budgetBaseUnits: 10_000_000n,
      expiry: 1_700_000_000,
      scopes: ["x402_pay", "trade_execute"],
      constitutionHash: BOB_CONSTITUTION,
    });
    const b = encodeSessionInstallData({
      signer: "0x000000000000000000000000000000000000beef" as `0x${string}`,
      sca: "0x000000000000000000000000000000000000cafe" as `0x${string}`,
      token: USDC_ARC_TESTNET,
      budgetBaseUnits: 10_000_000n,
      expiry: 1_700_000_000,
      scopes: ["trade_execute", "x402_pay"], // order swapped — sorted internally
      constitutionHash: BOB_CONSTITUTION,
    });
    expect(a).toEqual(b);
  });

  it("includes the constitutionHash bytes verbatim", () => {
    const data = encodeSessionInstallData({
      signer: "0x000000000000000000000000000000000000beef" as `0x${string}`,
      sca: "0x000000000000000000000000000000000000cafe" as `0x${string}`,
      token: USDC_ARC_TESTNET,
      budgetBaseUnits: 10_000_000n,
      expiry: 1_700_000_000,
      scopes: ["x402_pay"],
      constitutionHash: BOB_CONSTITUTION,
    });
    expect(data.toLowerCase()).toContain(BOB_CONSTITUTION.slice(2).toLowerCase());
  });
});

describe("demoConstitutionHash (helper)", () => {
  it("matches keccak256(toHex('agorahack-demo-constitution|<label>'))", () => {
    const label = "no-leverage";
    expect(demoConstitutionHash(label)).toEqual(
      keccak256(toHex(`agorahack-demo-constitution|${label}`)),
    );
  });
});
