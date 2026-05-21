# Orchestrator — Slice 5A

The Alice + Bob orchestrator that wires together everything Phase 1 shipped.
Implements spec §4.6 (Alice host), §4.7 (Bob naive agent), and §4.9 (the
six-step demo flow).

## Files owned by this slice

| File | Purpose |
|---|---|
| `agents/alice.py` | Seasoned agent — seeds 5K MiniLM-embedded trade traces + pins 3 constitution rules, hosts the dark pool |
| `agents/bob.py` | Naive agent — local EOA, constitution hash, x402 client, decision loop returning a `TradeIntent` |
| `agents/orchestrator.py` | Top-level glue — `Orchestrator(alice, bob).run_demo_step(n)` for steps 1..6 |
| `agents/seed_alice.py` | CLI: `python -m agents.seed_alice [--n 5000] [--force]` |
| `agents/tests/test_orchestrator.py` | End-to-end pytest |
| `agents/tests/conftest.py` | Pre-seeds the placeholder `/tmp/alice.mem` so `agents.dark_pool`'s eager module-level app load succeeds |

## Quick start

```bash
# from repo root
source agents/.venv/bin/activate
pip install -r agents/requirements-orchestrator.txt        # one-time

# pre-seed Alice (first run ~30s — downloads + runs MiniLM)
python -m agents.seed_alice --n 5000                       # → /tmp/alice.mem

# run the test suite
pytest agents/tests/test_orchestrator.py -v

# run the six-step demo end-to-end in-process
python - <<'PY'
from agents.alice import Alice
from agents.bob import Bob
from agents.orchestrator import Orchestrator, DEFAULT_BOB_RULES

alice = Alice(corpus_size=5000, mem_path="/tmp/alice.mem")
alice.bootstrap()
bob = Bob(budget_usdc=10.0, constitution_rules=DEFAULT_BOB_RULES,
          embedding_model="sentence-transformers/all-MiniLM-L6-v2")
bob.bootstrap()
orch = Orchestrator(alice, bob)
for n in range(1, 7):
    r = orch.run_demo_step(n)
    print(f"step {n} ok={r['ok']} {r['duration_ms']:.1f}ms — {r['name']}")
PY
```

## API contract

```python
# Alice
alice = Alice(corpus_size=5000, embedding_model="sentence-transformers/all-MiniLM-L6-v2", port=8001)
alice.bootstrap()            # idempotent — warm boot from /tmp/alice.mem if it exists
alice.dark_pool_url          # "http://127.0.0.1:8001"
alice.pinned_root            # bytes32 — Merkle root of pinned constitution slot
alice.client                 # fastapi.testclient.TestClient (in-process HTTP)

# Bob
bob = Bob(
    budget_usdc=10.0,
    constitution_rules=[
        {"rule_id": "MAX_TRADE_1USDC", "kind": "MAX_TRADE_SIZE", "max_usdc": 1.0},
        {"rule_id": "VENUE_BLACKLIST_DEAD", "kind": "VENUE_BLACKLIST",
         "venues": ["0x000000000000000000000000000000000000dEaD"]},
    ],
    embedding_model="sentence-transformers/all-MiniLM-L6-v2",  # optional but recommended
)
bob.bootstrap()              # generates EOA, hashes constitution, inits local memory
intent: TradeIntent = bob.decide(
    alice_url="", market_state="ETH funding flipped negative",
    transport=alice.client, trade_size_usdc=5.0,            # 5x cap → violator
)
# intent.target / intent.value / intent.calldata / intent.execute_calldata
# intent.kind == "MAX_TRADE_SIZE"
# intent.selector_hex() == "0xa9059cbb"  (real ERC-20 transfer selector)

# Orchestrator
orch = Orchestrator(alice, bob)
r = orch.run_demo_step(4)
# {
#   "step": 4, "name": "...", "ok": True, "duration_ms": 13.0,
#   "evidence": {"intent_kind": "MAX_TRADE_SIZE",
#                "inner_selector": "0xa9059cbb",
#                "execute_calldata_hex": "0xb61d27f6...",
#                "broadcast_handoff": "Slice 5D", ...},
#   "next_step_hint": "Slice 5D: submit execute_calldata_hex ...",
# }
```

## Demo step semantics (spec §4.9)

| Step | Name | What runs |
|---|---|---|
| 1 | Spawn Bob | Generate EOA + hash constitution; Alice bootstraps if needed |
| 2 | Bob queries Alice's dark pool | Real x402 EIP-712 round-trip via Slice 4's client; pays 0.001 USDC |
| 3 | Bob writes the new lesson | Top-1 trace recorded into Bob's local `MemoryService` |
| 4 | Bob attempts a violating trade | Builds `TradeIntent` (`execute(USDC, 0, transfer(beef, 5e6))`); HANDS OFF to Slice 5D |
| 5 | Memory decay | Advance time 30d; pinned constitution rules survive (root stable, hash equal) |
| 6 | Spawn child agent | Sub-budget Bob with inherited constitution; ERC-7715 issuance is Slice 5C |

Each step returns a structured dict; `evidence["..."]` contains the
artifacts (calldata, payment counts, trace ids) needed to assert what
happened. Step 4 is the load-bearing one for Slice 5D — its
`execute_calldata_hex` is what gets broadcast against
`ConstitutionHook.validateUserOp` on Arc.

## Real-data discipline

Per the project's "no fake/demo/test data" mandate:

- Embeddings: REAL `sentence-transformers/all-MiniLM-L6-v2` (384-d). First
  seed is ~13s on a laptop; warm boot from `/tmp/alice.mem` is <100ms.
- EOAs: REAL `eth_account.Account` instances generated from `secrets.token_hex(32)`.
- x402: REAL EIP-712 typed-data signing via `eth_account`, REAL Starlette
  HTTP round-trip via `fastapi.testclient.TestClient`. Server validates
  via `ecrecover`. Nonce store records every accepted payment — tests
  assert the count grew by exactly one.
- Pinned Merkle root: REAL Slice-1 implementation; bytes32, deterministic
  across save/load, byte-identical across process boots when the corpus
  + seed are constant.
- Constitution calldata: REAL `0xa9059cbb` ERC-20 transfer selector wrapped
  in REAL `0xb61d27f6` ERC-7579 `execute(address,uint256,bytes)`. NOT the
  made-up `setLeverage` / `issueSessionKey` selectors documented as a known
  gap in `docs/audit_phase1.md`.

## Known limitations

- **Step 4 does NOT broadcast.** It produces an `execute_calldata_hex` and
  hands it to Slice 5D. Step 4's `ok=True` means "intent built correctly";
  it does NOT mean "revert observed on chain". The brief explicitly scopes
  on-chain broadcast to Slice 5D.
- **No real Turnkey integration.** Bob's EOA is a local random key.
  Real Turnkey API integration is Slice 5C.
- **Child agent spawn is in-process.** Step 6 materialises a Python Bob
  with sub-budget; real ERC-7715 session-key issuance + on-chain
  enforcement is Slice 5C.
- **MAX_LEVERAGE rule is informational.** The hash includes a 2x leverage
  rule, but Bob's calldata never emits `setLeverage(uint256)` (the
  selector Slice 2 fakes), so that branch of the hook is dormant in this
  slice's demo. Production needs a real perp-DEX adapter; out of hackathon
  scope.

## How to extend this slice

- Need richer queries? Add intents under `Bob.build_*_intent()` (see the
  blacklisted-venue + non-whitelisted-contract helpers). Add a method on
  `Orchestrator` that exercises the new path.
- Need a different rule? Add the kind label to `_KIND_STR_TO_INT` and the
  encoding to `_encode_rule_params` in `agents/bob.py`. Update
  `_classify_intent` so the orchestrator can label evidence dicts.
- Need real network instead of in-process? Use
  `start_alice_subprocess(AliceConfig(...))` from `agents/alice.py` —
  preserved Slice-5D shim. Then pass `transport=None` to `Bob.query_alice`
  and a full `http://...` URL.

## Test status

- 10 new orchestrator tests pass with `AGORA_SKIP_FULL_CORPUS=1` in <30s.
- `test_alice_seeds_5k_real_text` runs the full 5K MiniLM corpus in ~21s.
- All 45 prior tests (memory, dark pool, nonce store, rate limiter) still
  pass.
- Total: **55 pytest pass** in the agents suite.
