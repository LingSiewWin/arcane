# Constrained Cognition — Contracts (Slice 2)

Solidity contracts that enforce an agent's constitution on Arc L1.

## Layout

```
contracts/
├── src/
│   ├── ConstitutionRegistry.sol     # content-addressed rule storage
│   ├── ConstitutionHook.sol         # ERC-7579 validator module
│   ├── MemoryAnchor.sol             # pinned-memory Merkle root anchor
│   ├── BondVault.sol                # stake + slash + release
│   └── interfaces/IERC7579.sol      # minimal validator interface
├── test/
│   ├── ConstitutionRegistry.t.sol
│   ├── ConstitutionHook.t.sol
│   ├── MemoryAnchor.t.sol
│   ├── BondVault.t.sol
│   └── mocks/MockERC20.sol
├── script/Deploy.s.sol              # Arc testnet deployment
└── foundry.toml
```

## Build + test

```sh
cd contracts
forge build
forge test -vv
```

37 tests, all passing.

## Contracts

### `ConstitutionRegistry`
Stores rule arrays keyed by `keccak256(abi.encode(rules))`. Same rules ⇒ same
hash ⇒ same storage slot, so the hash is the constitution's canonical id.

### `ConstitutionHook` (ERC-7579 validator)
Install with `onInstall(abi.encode(bytes32 constitutionHash))`. On every
user-op, decodes `callData` as `execute(target, value, innerData)` and walks
the rule list.

Supported rule kinds:

| kind | name                  | params                              |
|------|-----------------------|-------------------------------------|
| 0    | MAX_LEVERAGE          | `abi.encode(uint256 maxBps)`        |
| 1    | MAX_TRADE_SIZE        | `abi.encode(uint256 maxAmount)`     |
| 2    | VENUE_BLACKLIST       | `abi.encode(address[])`             |
| 3    | NO_UNAUDITED_CONTRACTS| `abi.encode(address[] whitelist)`   |
| 4    | SUBDELEGATION_BOUND   | `abi.encode(uint256 maxChildBudget)`|
| 255  | CUSTOM                | (ignored — extension point)         |

Recognised inner selectors:

| selector   | call                              | used by       |
|------------|-----------------------------------|---------------|
| `0x79575b23` | `setLeverage(uint256)`          | MAX_LEVERAGE  |
| `0xa9059cbb` | `transfer(address,uint256)`     | MAX_TRADE_SIZE|
| `0x7873af1d` | `issueSessionKey(address,uint256)` | SUBDELEGATION_BOUND |

A violation emits `ConstitutionViolation(agent, ruleId, reason)` and reverts
with `ConstitutionViolation:<KIND>`.

### `MemoryAnchor`
Per-agent commitment of the Merkle root over pinned memory entries. Emits
`MemoryAnchored(agent, root, timestamp)`. Anchors cost < 80k gas.

### `BondVault`
Reuses Circle arc-escrow's slash pattern:
- `post(amount)` — pulls bondToken via `transferFrom`.
- `slash(agent, amount)` — `onlyOracle`; forwards to `insurance`.
- `release()` — agent withdraws after the release window or when oracle calls
  `approveRelease(agent)`.
- `setOracle / setInsurance / setReleaseWindow` — `onlyOwner`.

USDC address is a constructor arg (Arc canonical:
`0x3600000000000000000000000000000000000000`).

## Deployment

The deploy script targets Arc testnet (chain id `5042002` / `0x4cef52`). It is
intentionally **not broadcast** here — integration phase will run it.

```sh
export ARC_RPC=https://rpc.testnet.arc.network
export DEPLOYER_PK=0x...
# optional overrides
export ARC_USDC=0x3600000000000000000000000000000000000000
export BOND_ORACLE=0x...
export BOND_INSURANCE=0x...
export BOND_WINDOW_SECS=604800

forge script script/Deploy.s.sol:Deploy \
  --rpc-url $ARC_RPC \
  --private-key $DEPLOYER_PK \
  --broadcast --slow
```

The script prints all four addresses; plug them into the orchestrator's env.

## Notes / non-goals

- No batched `executeBatch` support in the hook; only single `execute` call.
- ERC-1271 `isValidSignatureWithSender` returns `0xffffffff` (unsupported).
- No upgrade path; redeploy + re-install if rules change shape.
