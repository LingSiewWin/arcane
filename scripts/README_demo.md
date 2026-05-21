# Constrained Cognition — demo runbook (Slice 5D)

This is the runbook for the end-to-end demo. It composes:

- Slice 1 / 5A — `MemoryService` + Alice's seeded dark pool memory
- Slice 2 — the four Solidity contracts (`ConstitutionRegistry`,
  `ConstitutionHook`, `MemoryAnchor`, `BondVault`)
- Slice 4 — Alice's x402 dark pool server + Bob's x402 client
- Slice 5A — Alice + Bob + Orchestrator
- Slice 5D — *this slice* — deployment + on-chain orchestration

## Components

| File | What it does |
|---|---|
| `scripts/deploy_arc.sh` | bash wrapper around `forge script` |
| `scripts/seed_alice.py` | seeds `/tmp/alice.mem` with canned traces + pinned rules |
| `scripts/anchor_memory.py` | `MemoryAnchor.anchor(bytes32)` for Alice's pinned root |
| `scripts/demo_e2e.py` | 6-step demo runner — `--mode local` or `--mode live` |
| `scripts/lib/chain.py` | tiny RPC helpers (cast wrappers + raw JSON-RPC) |
| `scripts/tests/` | pytest suite (4 test files, 6 tests) |
| `deployments/arc-testnet.json` | populated after a successful broadcast |

The demo never touches any KB. All Circle/Arc references are public.

## Quickstart — local mode (no real broadcast)

This runs against an `anvil --fork-url $RPC` local fork. Free, fast, hermetic.
Requires foundry (`anvil`, `forge`, `cast`) on PATH.

```sh
# 1. Make sure $RPC is in your shell (sourced from ~/.arc-canteen/env in the
#    script — you don't have to source it yourself).
# 2. Build the contracts once:
cd contracts && forge build && cd ..

# 3. Run the demo:
agents/.venv/bin/python -m scripts.demo_e2e --mode local
```

Output is appended to `scripts/demo_output.jsonl` (truncated at start of each
run). Six lines, one per step. Example:

```jsonl
{"step": 1, "name": "spawn_bob",                    "ok": true, ...}
{"step": 2, "name": "query_alice",                  "ok": true, ...}
{"step": 3, "name": "select_violating_trace",       "ok": true, ...}
{"step": 4, "name": "constitution_revert",          "ok": true, ...}   # receipt_status=0 ✓
{"step": 5, "name": "anchor_pinned_root",           "ok": true, ...}   # MemoryAnchored event ✓
{"step": 6, "name": "spawn_child_and_bond_resolve", "ok": true, ...}
```

Step 4's `ok=true` means **the revert was observed** — that's the success
condition. The tx hash + revert reason are in `evidence.revert_reason`.

## Live mode — real Arc testnet

> **This costs faucet USDC.** ~0.10 USDC for the four deploys + the six
> tx-level operations on Arc testnet.
>
> Get faucet USDC from <https://faucet.circle.com> for your deployer address
> BEFORE running this.

```sh
# 1. Funding check (run `cast wallet address --private-key $DEPLOYER_PK` if
#    you forget the address):
cast balance --rpc-url $RPC <YOUR_DEPLOYER_ADDR>

# 2. Run the demo. The --yes-i-understand flag is mandatory and the script
#    refuses to broadcast without it.
RPC=...  DEPLOYER_PK=0x...  \
    agents/.venv/bin/python -m scripts.demo_e2e \
        --mode live --yes-i-understand
```

The same `scripts/demo_output.jsonl` is produced — each step's `explorer_url`
field now points at <https://testnet.arcscan.app>.

### Live mode safety gates

- `--mode live` without `--yes-i-understand` → exit non-zero, no tx sent
- `--mode live` without `$DEPLOYER_PK` (or `--pk`) → exit non-zero, no tx sent
- `--mode live` without `$RPC` (or `--rpc-url`) → exit non-zero, no tx sent
- All three above are enforced BEFORE any subprocess starts (`run_live()` in
  `scripts/demo_e2e.py`).

## What the 6 steps prove

| # | Step | Demo claim | Evidence in JSONL |
|---|------|-----------|-------------------|
| 1 | spawn Bob | Bob has an EOA, budget, constitution rules registered on chain | `tx_hash` = ConstitutionRegistry.defineConstitution tx |
| 2 | Bob pays + queries Alice | x402 dance completes; 0.001 USDC authorization signed | `n_results > 0`; nonce stored in Alice's nonce store |
| 3 | Pick violating trace | Top-1 trace recommends an oversized trade | `selected_text` contains a "size N" >1 USDC |
| 4 | Attempt trade → revert | ConstitutionHook reverts the user-op | `tx_hash` with `receipt_status: 0` + `revert_reason: "ConstitutionViolation:MAX_TRADE_SIZE"` |
| 5 | Decay + anchor | Working entries evicted; pinned root unchanged; root anchored on chain | `evicted > 0`; `pinned_root_before == pinned_root_after`; `MemoryAnchored` event |
| 6 | Spawn child + bond | Child has sub-budget + inherited constitution; bond posted + slashed | `child_eoa`; `bond_post` + `slash_tx` tx hashes |

## Tests

```sh
agents/.venv/bin/python -m pytest scripts/tests/ -v
```

| Test | Asserts |
|---|---|
| `test_local_mode_runs_e2e.py` | 6 JSONL lines, all `ok=True`, step 4 reverts, step 5 anchors |
| `test_live_mode_requires_explicit_flag.py` | live mode refuses without `--yes-i-understand` AND without DEPLOYER_PK |
| `test_anchor_memory_emits_event.py` | `anchor_memory` produces a real `MemoryAnchored` event on an anvil fork |
| `test_deploy_arc_dry_run.py` | `deploy_arc.sh` dry-run lists 4 contracts; broadcast without PK fails clean |

## Known limitations

- **Step 2 settlement is signature-only.** The x402 EIP-3009 authorization is
  signed + validated but never settled on chain (Circle Gateway batching is
  separate). See `docs/audit_phase1.md` slice 4.
- **Step 4 bypasses ERC-4337.** We call `ConstitutionHook.validateUserOp`
  directly rather than via an EntryPoint. The revert reason + event are the
  same; only the dispatch path differs.
- **Step 6 bond flow in local mode** relies on the deployer being preloaded
  with USDC. On anvil's empty chain this fails; on a true `--fork-url` chain
  the deployer is anvil's default account #0 which has 10000 ETH but no USDC
  unless a faucet pre-funded it. In production the demo wallet must hold
  >= 1 USDC.

## Updating the constitution

The demo's default constitution is in `agents.orchestrator.default_bob_rules`.
To change it, edit that function — the constitution hash, the on-chain
`defineConstitution` call, Bob's pinned memory, and the violating-trade
selection all key off the same shape automatically.
