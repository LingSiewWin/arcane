# AgoraHack — real-broadcast runbook

This is the runbook for **broadcasting the full 6-step demo to real Arc
testnet**. The local-mode `anvil --fork-url` demo runs against forked state;
this runbook walks you through actually spending faucet USDC and producing
transactions visible on <https://testnet.arcscan.app>.

> If you only want to validate the demo against forked state, see
> `scripts/README_demo.md` — that does NOT require a deployer key or any
> USDC. This document is for the live-fire variant.

## TL;DR

```sh
# 1. Validate everything is ready (no USDC spent here)
RPC=<your-arc-rpc> DEPLOYER_PK=0x<your-32-byte-key> \
    agents/.venv/bin/python -m scripts.preflight --mode live

# 2. Go live (only if preflight is GREEN)
RPC=<your-arc-rpc> DEPLOYER_PK=0x<your-32-byte-key> \
    agents/.venv/bin/python -m scripts.demo_e2e \
        --mode live --yes-i-understand
```

## What goes on-chain

The live demo broadcasts the following transactions in order. Total cost is
roughly **0.10 USDC** for gas plus **0.001 USDC** for Bob's x402 payment
plus **1 USDC** for the bond — call it **~1.2 USDC** start-to-finish.

| Step | Tx type | Counterparty | USDC cost (approx) |
|------|---------|--------------|-------------------:|
| Setup | `ConstitutionRegistry` deploy | new contract | 0.02 |
| Setup | `ConstitutionHook(registry)` deploy | new contract | 0.02 |
| Setup | `MemoryAnchor` deploy | new contract | 0.02 |
| Setup | `BondVault(usdc, owner, oracle, win)` deploy | new contract | 0.02 |
| 1 | `defineConstitution(rules)` | ConstitutionRegistry | 0.01 |
| 4 | `onInstall(constitutionHash)` | ConstitutionHook | 0.005 |
| 4 | `validateUserOp(...)` (reverts on purpose) | ConstitutionHook | 0.005 |
| 5 | `anchor(bytes32)` (emits `MemoryAnchored`) | MemoryAnchor | 0.005 |
| 6 | `approve(vault, 1e6)` | USDC | 0.005 |
| 6 | `post(1e6)` | BondVault | 0.005 |
| 6 | `slash(deployer, 1e5)` | BondVault | 0.005 |
| **Total** | | | **~0.12 USDC** |

The faucet drips 2 USDC at a time; one drip is enough to run the full demo
twice. Preflight's USDC threshold defaults to 2 USDC for headroom.

Faucet USDC, deployer EOAs, contract addresses, the dark pool, Alice's
memory, and Bob's spawn flow are all isolated to the deployer EOA you
provide — no other agent's funds or identity are touched.

## Step-by-step

### 1. Get a deployer key

You have two options:

**Generate a fresh key:**

```sh
cast wallet new
# prints:  Successfully created new keypair.
# Address:     0x1234...
# Private key: 0xabcd...        <-- this is your DEPLOYER_PK
```

**Import an existing key:**

```sh
export DEPLOYER_PK=0xabcdef...      # 64 hex chars after the 0x
```

The private key never leaves your machine. The preflight check derives the
EOA address from it locally and only prints the public address, never the
key itself.

### 2. Fund the deployer from the faucet

1. Open <https://faucet.circle.com> in a browser
2. Select **Arc Testnet** from the chain dropdown
3. Paste the EOA address from step 1 (the public 0x… not the private key)
4. Request 2 USDC (one drip)
5. Wait ~30 seconds for the tx to confirm

Verify on the explorer:

```sh
cast call --rpc-url $RPC \
    0x3600000000000000000000000000000000000000 \
    'balanceOf(address)(uint256)' \
    <YOUR_DEPLOYER_ADDR>
# expect: 2000000  (= 2 USDC at 6 decimals)
```

Or just re-run preflight — it does this check for you.

### 3. Set env vars

```sh
# RPC: source from ~/.arc-canteen/env if you've set that up,
# otherwise use the canonical public Arc testnet RPC.
export RPC=https://rpc.testnet.arc.network
export DEPLOYER_PK=0xabcdef...
```

### 4. Run preflight

```sh
agents/.venv/bin/python -m scripts.preflight --mode live
```

Expected output when ready to broadcast:

```
=== AgoraHack -- pre-flight check ===
mode: live    chain id (Arc Testnet): 5042002

[GREEN]  anvil Version: 1.5.1-stable
[GREEN]  forge Version: 1.5.1-stable
[GREEN]  cast Version: 1.5.1-stable
[GREEN]  Python venv ready (/path/to/agents/.venv/bin/python)
[GREEN]  @circle-fin/x402-batching installed
[GREEN]  All 4 contract artifacts present
[GREEN]  Demo output writable (/path/to/scripts/demo_output.jsonl)
[GREEN]  Alice memory seeded (5,003 entries, 3 pinned)
[GREEN]  No stale nonce DB at /tmp/darkpool_nonces.db
[GREEN]  RPC URL available (source: os.environ)
[GREEN]  Arc Testnet RPC reachable (chain 5042002, block 43,299,360)
[GREEN]  DEPLOYER_PK is set (32 bytes, 0x-prefixed)
[GREEN]  Deployer EOA: 0x1234...
[GREEN]  USDC balance: 2.000000 USDC (>= 2.00 required)

SUMMARY: 14 green, 0 yellow, 0 red.

All checks pass.

Safe to broadcast. Run:

    RPC=$RPC \
    DEPLOYER_PK=$DEPLOYER_PK \
    /path/to/agents/.venv/bin/python -m scripts.demo_e2e \
        --mode live --yes-i-understand

After broadcast, verify on the explorer:
    https://testnet.arcscan.app/address/0x1234...
```

Exit codes:

- `0` — GREEN, safe to broadcast
- `1` — YELLOW, will work but deviates from audited path (use `--strict` to
  treat as failure)
- `2` — RED, broadcast will fail. Fix the issues and re-run.

### 5. (Optional) Dry-run the deploy

The `deploy_arc.sh` script prints what it would do without broadcasting:

```sh
scripts/deploy_arc.sh         # dry-run (default)
```

The full live demo also runs `deploy_arc.sh`'s logic — you don't need to run
this separately. It exists for when you want to deploy the contracts
without running the orchestrator (e.g., reusing them across many demo runs).

### 6. Run the live demo

```sh
agents/.venv/bin/python -m scripts.demo_e2e --mode live --yes-i-understand
```

The demo prints a 3-second countdown before sending any tx — Ctrl-C aborts
cleanly. Once it starts:

1. Deploys the 4 contracts to Arc
2. Registers Bob's constitution
3. Bob pays + queries Alice's dark pool
4. Bob's constitution-violating tx reverts (this is the success condition)
5. Alice anchors her pinned-memory root on-chain
6. A child SCA is spawned and the bond gets slashed + resolved

Each step writes one JSONL line to `scripts/demo_output.jsonl` with a
`tx_hash` and an `explorer_url`.

### 7. Verify on the explorer

After the demo finishes:

```sh
# Inspect the 6 tx hashes
jq -r '.tx_hash // empty' scripts/demo_output.jsonl
```

Open each `explorer_url` from the JSONL — every successful step should show
a confirmed tx on <https://testnet.arcscan.app>. Step 4's tx should show
`Failed` status with a `ConstitutionViolation:MAX_TRADE_SIZE` revert reason
(that's the demo's point: the constitution prevented a forbidden trade).

## Recovery — what to do when preflight goes RED

### `DEPLOYER_PK is not set`

You haven't exported the key into the current shell. Re-run with:

```sh
export DEPLOYER_PK=0xabcdef...
agents/.venv/bin/python -m scripts.preflight --mode live
```

### `DEPLOYER_PK must start with 0x` / `is not a valid 32-byte hex key`

The key is malformed. Check that you have exactly 64 hex chars after
the `0x` prefix (no whitespace, no quotes). Regenerate with `cast wallet
new` if unsure.

### `USDC balance: 0.00 USDC — need 2.0 USDC minimum`

Visit <https://faucet.circle.com>, select Arc Testnet, request 2 USDC for
your deployer address. Wait ~30 seconds and re-run preflight.

If the faucet says "rate limited", wait a few minutes — Circle's faucet
has anti-abuse throttling.

### `RPC timed out` / `RPC unreachable`

The Arc testnet RPC is either down, or your `$RPC` URL is malformed/expired.
Try the canonical public RPC:

```sh
export RPC=https://rpc.testnet.arc.network
agents/.venv/bin/python -m scripts.preflight --mode live
```

If the canonical RPC is also down, check the Arc status page or wait — there
is nothing you can fix locally.

### `Chain id mismatch: got X, expected 5042002`

Your `$RPC` is pointing at the wrong network. **Do not proceed** — broadcasting
against the wrong chain will lose your USDC. Re-set:

```sh
export RPC=https://rpc.testnet.arc.network
```

### `Alice memory not found at /tmp/alice.mem`

Seed the dark pool memory:

```sh
agents/.venv/bin/python -m agents.seed_alice
# takes ~30s the first time (downloads sentence-transformers model)
```

### `Alice memory has only N entries (< 5000)`

The current memory is undersized — demo will still run but recall will be
lower than the audited 92%. Rebuild:

```sh
agents/.venv/bin/python -m agents.seed_alice --force
```

### `Stale nonce DB at /tmp/darkpool_nonces.db has N rows`

A previous demo run left replay-protection state behind. If you're using
the SAME deployer EOA again, this is fine. If you're using a NEW EOA (or
got a fresh faucet drip), remove it to avoid potential replay rejections:

```sh
rm /tmp/darkpool_nonces.db
```

### `anvil/forge/cast not found on PATH`

Install Foundry:

```sh
curl -L https://foundry.paradigm.xyz | bash
foundryup
```

### `Python venv missing dependencies`

```sh
agents/.venv/bin/pip install -r agents/requirements-darkpool.txt
agents/.venv/bin/pip install -r agents/requirements-memory.txt
agents/.venv/bin/pip install -r agents/requirements-orchestrator.txt
```

### `@circle-fin/x402-batching not installed`

```sh
pnpm install
```

### `Contract artifacts missing`

```sh
cd contracts && forge build && cd ..
```

## Recovery — what to do when the demo itself fails

The `--mode live` demo can fail mid-run even when preflight is GREEN. Common
causes:

### Step 1 reverts with `OwnableUnauthorizedAccount`

The deployer EOA you used isn't the one that originally deployed the
`ConstitutionRegistry`. Each demo run deploys fresh contracts; you should
not see this on a clean run. If you do, it means a previous run's
`deployments/arc-testnet.json` is being reused inappropriately — delete it
and try again.

### Step 4 fails to revert (receipt_status=1 instead of 0)

Bob's trade calldata isn't triggering the hook's MAX_TRADE_SIZE rule. This
is usually a sign that:

- the constitution hash on-chain differs from Bob's local rules (check step 1
  evidence — `constitution_hash_onchain` should equal `constitution_hash_local`),
- or the hook wasn't installed on Bob's SCA before the trade attempt.

Check `scripts/demo_output.jsonl` step 4 evidence for the
`hook_install_tx`. If it's null, re-run — the install can race with the
revert tx on slow RPCs.

### Step 6 bond flow fails

The bond `slash()` call requires the deployer to be both the bond owner AND
the oracle. In Phase 2 the demo wires this automatically (BondVault is
constructed with `deployer` as both addresses). If you see "not oracle" or
"not owner" errors, the contract was deployed with a different account.
Re-run from scratch.

### Demo hangs

Open <https://testnet.arcscan.app/address/{your_deployer}> in a browser and
check pending txs. If anvil/forge is queuing locally, it's stuck on RPC; if
the explorer shows pending txs, Arc itself is slow — wait a minute and
retry.

## Wallet safety

- The preflight script never prints your private key.
- The deploy script (`scripts/deploy_arc.sh`) passes the key to forge via the
  `PRIVATE_KEY` env var, not as a CLI flag, so it doesn't appear in `ps`.
- All other tooling uses `cast send --private-key $DEPLOYER_PK` directly;
  the key stays in env vars throughout.
- Mainnet broadcasting is impossible: Arc is testnet-only and chain id
  5042002 is hardcoded as the only acceptable target.

If you lose your deployer key, you lose access to whatever USDC was on it.
The faucet will refund 2 USDC for a new EOA — just generate a new key with
`cast wallet new` and re-fund.

## What "done" looks like

After a successful live run, `scripts/demo_output.jsonl` contains six lines.
Each `tx_hash` resolves to a confirmed tx on
<https://testnet.arcscan.app>. Step 4's tx is **expected to be a failure**
— that's the constitution doing its job. Steps 1, 5, 6 should all be
successes with non-empty `evidence` blocks.

If you can paste the contents of `scripts/demo_output.jsonl` and every
explorer URL resolves correctly, the demo broadcast is **done** in the
sense of the CLAUDE.md Done definition: real end-to-end execution proven
by on-chain artifacts.
