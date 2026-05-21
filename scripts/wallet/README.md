# `scripts/wallet/` — Slice 3: Wallet Stack

> Turnkey EOA → Circle SCA → ConstitutionHook install → ERC-8004 identity → ERC-7715 session key.

This slice owns spinning up a new agent's full on-chain identity + spending
authorization in one call. It is consumed by Slice 1 (`agents/bob.py`,
`agents/alice.py`) and Slice 4 (`scripts/demo_e2e.py`).

## Files

| File | Role |
|---|---|
| `spawn_agent.ts` | CLI + `spawnAgent({...})` programmatic entry point |
| `turnkey_client.ts` | Turnkey-or-local EOA generator (TEE-backed when env is set) |
| `circle_sca.ts` | Circle Developer-Controlled SCA wrapping + `installModule` calldata builder |
| `erc8004_mint.ts` | `register(string)` against Arc's `0x8004A8…` IdentityRegistry |
| `erc7715_session.ts` | Session-key issuance + sub-delegation bounds check |
| `types.ts` | `AgentSpawnResult`, `SessionKeyAuth`, error types |
| `tests/spawn_agent.test.ts` | Vitest suite (dry-run, no RPC) |

## Quickstart

```bash
pnpm install
node node_modules/vitest/vitest.mjs run scripts/wallet/tests   # 12 tests, ~750ms
```

CLI dry-run:

```bash
node node_modules/tsx/dist/cli.mjs scripts/wallet/spawn_agent.ts \
  --name bob \
  --budget 10 \
  --expiry-min 5 \
  --constitution-hash 0xabcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789
```

Output (single-line JSON to stdout, human log to stderr):

```json
{
  "scaAddress": "0x19d2…D50",
  "identityId": "21382873",
  "sessionKey": {
    "signer": "0x9040…382",
    "sca":    "0x19d2…D50",
    "budgetUSDCBaseUnits": "10000000",
    "expiry": 1779340504,
    "scopes": ["x402_pay", "trade_execute", "memory_anchor"],
    "constitutionHash": "0xabc…789",
    "installData": "0x…"
  },
  "turnkeyEoa": "0x9040…382",
  "txHashes": {}
}
```

In dry-run mode the SCA address is deterministically derived from the EOA
plus the agent name, the ERC-8004 token id is a stub (low 30 bits of
`scaAddress XOR constitutionHash`), and `txHashes` is empty.

## Programmatic API

```ts
import { spawnAgent, demoConstitutionHash } from "./spawn_agent.js";

const result = await spawnAgent({
  name: "bob",
  budget_USDC: 10,
  expiryMinutes: 5,
  constitutionHash: demoConstitutionHash("no-leverage-above-2x"),
  // optional:
  // parentSessionKey, scopes, metadataURI, dryRun: false
});
```

Sub-delegation:

```ts
const child = await spawnAgent({
  name: "bob-child",
  budget_USDC: 5,            // <= parent.budget
  expiryMinutes: 5,          // <= parent expiry
  constitutionHash: parent.sessionKey.constitutionHash,
  parentSessionKey: parent.sessionKey,
});
```

Exceeding the parent throws `SubdelegationExceedsParentBounds` (matching
the contract-side revert reason in Slice 2's `ConstitutionHook.sol`).

## Going live

1. Set Turnkey credentials:
   - `TURNKEY_API_PUBLIC_KEY`
   - `TURNKEY_API_PRIVATE_KEY`
   - `TURNKEY_ORG_ID`
   - (optional) `TURNKEY_API_BASE_URL` (defaults to `https://api.turnkey.com`)
2. Set Circle Developer-Controlled Wallets credentials:
   - `CIRCLE_API_KEY` (format `TEST_API_KEY:...` for testnet)
   - `CIRCLE_ENTITY_SECRET` (register first at https://developers.circle.com/wallets/dev-controlled/register-entity-secret)
3. Set `RPC` to Arc Testnet RPC (`https://rpc.testnet.arc.network` is the default).
4. Set `CONSTITUTION_HOOK_ADDRESS` to the deployed `ConstitutionHook.sol`
   address from Slice 2. The default placeholder `0xC0775770…C0DEC0DE` is
   intentionally invalid for production.
5. Pass `--dry-run=false` (or `dryRun: false`).
6. Make sure the SCA has Arc testnet USDC for gas (from
   https://faucet.circle.com — gas ≈ 0.006 USDC per tx).

## Known limitations

- Circle's deployed SCA implementation does NOT currently expose ERC-7715
  natively. We materialize the permission via an admin
  `setSpendingLimit(address,uint256,uint256,address,bytes32)` call as the
  documented placeholder. When 7715 ships, only `erc7715_session.ts` needs
  to change — the `SessionKeyAuth` payload shape is already 7715-shaped.
- The ConstitutionHook `installModule(uint256,address,bytes)` signature
  follows the canonical ERC-7579 selector `0x9517e29f`. If Circle's MSCA
  implementation deviates, `circle_sca.ts::encodeConstitutionInstallData`
  is the single edit point.
- ERC-8004 mint uses Arc's actual ABI (`register(string metadataURI)`),
  NOT the spec's `register(address controller, bytes32 metadataHash)`.
  Token id is resolved via the `Transfer` event log.
