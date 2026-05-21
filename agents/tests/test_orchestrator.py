"""End-to-end tests for Slice 5A — Alice + Bob + Orchestrator.

Real-data discipline (per the brief):
  * Alice's MemoryService is the REAL Slice-1 service. Pinned rules are
    actual ``MemoryService.add(... pinned=True)`` entries; the Merkle
    root is the genuine Slice-1 implementation.
  * x402 round-trip is REAL: Bob's EOA is an actual ``eth_account.Account``,
    EIP-712 signing happens inside ``x402_client.x402_query``, the server
    validates via ``ecrecover``. Transport is ``fastapi.testclient.TestClient``
    so requests cross Starlette's full middleware stack — no HTTP mocks.
  * Embeddings are REAL ``sentence-transformers/all-MiniLM-L6-v2`` for the
    5K-corpus test; smaller tests downscale corpus_size to keep CI fast.

Tests cover the 5 contracts from the brief:

  test_alice_seeds_5k_real_text            — pinned slot Merkle root non-zero
  test_bob_bootstrap_creates_eoa_and_constitution — EOA + keccak match
  test_bob_decides_uses_real_x402_round_trip      — 1 payment recorded
  test_demo_step_1_through_6                       — all 6 steps return ok=True
  test_constitution_calldata_uses_real_selectors   — 0xa9059cbb appears in calldata
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import numpy as np
import pytest
from eth_account import Account
from eth_utils import keccak

from agents.alice import (
    Alice,
    ConstitutionRule,
    DEFAULT_EMBED_DIM,
    DEFAULT_PINNED_RULES,
    make_corpus,
)
from agents.bob import (
    Bob,
    ERC20_TRANSFER_SELECTOR,
    EXECUTE_SELECTOR,
    TradeIntent,
    constitution_hash,
    rules_to_solidity,
)
from agents.orchestrator import (
    DEFAULT_BOB_RULES,
    Orchestrator,
    default_bob_rules,
    hash_constitution,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


SMALL_CORPUS = int(os.environ.get("AGORA_TEST_CORPUS_SIZE", "120"))
FULL_CORPUS = int(os.environ.get("AGORA_FULL_CORPUS_SIZE", "5000"))
TEST_MEM_PATH = "/tmp/alice_orch_test.mem"
FULL_MEM_PATH = "/tmp/alice_orch_full.mem"


@pytest.fixture(scope="module")
def small_alice() -> Alice:
    """Alice bootstrapped with a small REAL-embedding corpus.

    120 entries is enough to verify all interface invariants without paying
    the full 30s embedding cost for every test. ``test_alice_seeds_5k_real_text``
    is the one test that runs the full 5K corpus and is allowed to be slow.
    """
    if Path(TEST_MEM_PATH).exists():
        Path(TEST_MEM_PATH).unlink()
    a = Alice(
        corpus_size=SMALL_CORPUS,
        mem_path=TEST_MEM_PATH,
        seed=2026,
    )
    a.bootstrap()
    return a


@pytest.fixture
def fresh_bob() -> Bob:
    bob = Bob(budget_usdc=10.0, constitution_rules=DEFAULT_BOB_RULES)
    bob.bootstrap()
    return bob


# ---------------------------------------------------------------------------
# Contract 1: Alice seeds 5K REAL-text entries + 3 pinned rules
# ---------------------------------------------------------------------------


def test_alice_seeds_5k_real_text():
    """Full 5K seed using REAL sentence-transformers embeddings.

    This test is the only one that touches the full corpus. It's marked
    slow but always runs by default — the brief's done-definition requires
    REAL embeddings, not a mocked path. To skip locally, set
    ``AGORA_SKIP_FULL_CORPUS=1``.
    """
    if os.environ.get("AGORA_SKIP_FULL_CORPUS"):
        pytest.skip("AGORA_SKIP_FULL_CORPUS=1 set")

    if Path(FULL_MEM_PATH).exists():
        Path(FULL_MEM_PATH).unlink()

    alice = Alice(
        corpus_size=FULL_CORPUS,
        mem_path=FULL_MEM_PATH,
        seed=2026,
    )
    alice.bootstrap()

    # Working entries + pinned rules.
    assert len(alice.memory) == FULL_CORPUS + len(DEFAULT_PINNED_RULES)
    # Pinned entries are exactly the rules we declared.
    pinned = alice.memory.pinned_ids()
    assert len(pinned) == len(DEFAULT_PINNED_RULES)
    assert set(pinned) == {f"pinned:{r.rule_id}" for r in DEFAULT_PINNED_RULES}

    # Merkle root must be 32 bytes and non-zero (zero only if pinned set is empty).
    root = alice.pinned_root
    assert isinstance(root, bytes) and len(root) == 32
    assert root != b"\x00" * 32

    # Sample query — embed a probe through MiniLM (same model used at seed)
    # and verify it returns sensible results from the real corpus.
    from sentence_transformers import SentenceTransformer

    model = SentenceTransformer("sentence-transformers/all-MiniLM-L6-v2")
    probe = "short JTO on Hyperliquid because funding rate flipped negative"
    qvec = model.encode([probe], normalize_embeddings=True)[0].astype(np.float32)
    hits = alice.memory.query(vec=qvec, k=5)
    assert len(hits) == 5
    # Every hit must be a real working entry (not a pinned slot — those have
    # near-orthogonal hash-derived vectors).
    for tid, _score in hits:
        assert tid.startswith("t") and not tid.startswith("pinned:")


def test_alice_pinned_root_is_stable_across_save_load(small_alice: Alice):
    """Persisting + reloading yields the same Merkle root (Slice-1 contract)."""
    root_before = small_alice.pinned_root
    # Save to a scratch path and reload.
    scratch = "/tmp/alice_orch_scratch.mem"
    small_alice.memory.save(scratch)
    from agents.memory_service import MemoryService

    reloaded = MemoryService.load(scratch)
    root_after = reloaded.pinned_merkle_root()
    assert root_before == root_after


def test_alice_pinned_root_is_non_zero(small_alice: Alice):
    root = small_alice.pinned_root
    assert isinstance(root, bytes) and len(root) == 32
    assert root != b"\x00" * 32


# ---------------------------------------------------------------------------
# Contract 2: Bob's bootstrap produces real EOA + correct keccak
# ---------------------------------------------------------------------------


def test_bob_bootstrap_creates_eoa_and_constitution(fresh_bob: Bob):
    # EOA: well-formed 0x-prefixed 40-hex-char address.
    addr = fresh_bob.address
    assert addr.startswith("0x") and len(addr) == 42
    # Recovering via eth_account confirms the EOA is real (round-trip sign/verify).
    from eth_account.messages import encode_defunct

    msg = encode_defunct(text="agorahack-test")
    sig = fresh_bob.eoa.sign_message(msg)
    recovered = Account.recover_message(msg, signature=sig.signature)
    assert recovered == addr

    # Constitution hash matches manual keccak.
    sol_rules = rules_to_solidity(fresh_bob.constitution_rules)
    from eth_abi import encode as abi_encode

    expected = "0x" + keccak(
        abi_encode(["(uint8,bytes)[]"], [sol_rules])
    ).hex()
    assert fresh_bob.constitution_hash == expected


def test_bob_constitution_hash_module_helpers_agree(fresh_bob: Bob):
    # Three ways to compute the same hash. All must agree.
    h1 = fresh_bob.constitution_hash
    h2 = constitution_hash(fresh_bob.constitution_rules)
    h3 = hash_constitution(fresh_bob.constitution_rules)
    assert h1 == h2 == h3


# ---------------------------------------------------------------------------
# Contract 3: Bob.decide() exercises the REAL x402 dance
# ---------------------------------------------------------------------------


def test_bob_decides_uses_real_x402_round_trip(small_alice: Alice):
    """Bob.decide() must trigger exactly one x402 payment + return a real intent."""
    bob = Bob(budget_usdc=10.0, constitution_rules=DEFAULT_BOB_RULES)
    bob.bootstrap()

    nonce_store = small_alice.server._nonce_store
    n_before = len(nonce_store)

    intent = bob.decide(
        alice_url="",
        market_state="ETH funding flipped negative, considering short on Hyperliquid",
        transport=small_alice.client,
        chain_id=small_alice.chain_id,
        asset_address=small_alice.usdc_address,
        max_amount_usdc=small_alice.price_usdc,
        trade_size_usdc=5.0,  # 5x the 1 USDC cap — guaranteed violator
    )

    # Exactly one new x402 payment recorded.
    assert len(nonce_store) == n_before + 1

    # Intent shape — execute() outer wrapping transfer() inner.
    assert isinstance(intent, TradeIntent)
    assert intent.execute_calldata.startswith(EXECUTE_SELECTOR)
    assert intent.calldata.startswith(ERC20_TRANSFER_SELECTOR)
    assert intent.kind == "MAX_TRADE_SIZE"
    assert intent.source_trace_id is not None


# ---------------------------------------------------------------------------
# Contract 4: All 6 demo steps return ok=True
# ---------------------------------------------------------------------------


def test_demo_step_1_through_6(small_alice: Alice):
    """Run every demo step in sequence; assert ok=True on each."""
    # Fresh Bob so test isolation holds.
    bob = Bob(budget_usdc=10.0, constitution_rules=DEFAULT_BOB_RULES)
    bob.bootstrap()
    orch = Orchestrator(small_alice, bob)

    results = {}
    for step in range(1, 7):
        r = orch.run_demo_step(step)
        results[step] = r
        assert r["step"] == step, r
        assert r["ok"] is True, f"step {step} not ok: {r}"
        assert "evidence" in r and isinstance(r["evidence"], dict)
        assert "duration_ms" in r and r["duration_ms"] >= 0

    # Step 1 — constitution hash present + bob bootstrapped.
    assert results[1]["evidence"]["constitution_hash"].startswith("0x")
    # Step 2 — 1 x402 payment processed.
    assert results[2]["evidence"]["x402_payments_recorded"] == 1
    # Step 3 — Bob's memory grew by exactly the recorded lesson.
    e3 = results[3]["evidence"]
    assert e3["bob_memory_entries_after"] == e3["bob_memory_entries_before"] + 1
    # Step 4 — TradeIntent ready, real transfer selector.
    e4 = results[4]["evidence"]
    assert e4["inner_selector"] == "0xa9059cbb"
    assert e4["intent_kind"] == "MAX_TRADE_SIZE"
    assert e4["broadcast_handoff"] == "Slice 5D"
    # Step 5 — pinned root stable; some working entries evicted.
    e5 = results[5]["evidence"]
    assert e5["pinned_root_before"] == e5["pinned_root_after"]
    assert e5["pinned_before"] == e5["pinned_after"]
    assert e5["evicted"] >= 1
    # Step 6 — child inherits the constitution hash.
    e6 = results[6]["evidence"]
    assert e6["constitution_inherited"] is True
    assert e6["child_eoa"] != e6["parent_eoa"]
    assert e6["child_budget_usdc"] <= e6["parent_budget_usdc"]


# ---------------------------------------------------------------------------
# Contract 5: TradeIntent calldata uses REAL selectors (not made-up ones)
# ---------------------------------------------------------------------------


def test_constitution_calldata_uses_real_selectors(small_alice: Alice):
    """Bob's MAX_TRADE_SIZE TradeIntent must use the REAL transfer selector.

    Per docs/audit_phase1.md, Slice 2's hook fires on real ``transfer(address,
    uint256)`` (0xa9059cbb) for MAX_TRADE_SIZE; the brief mandates we
    construct calldata around the REAL selector and NOT use the made-up
    ``setLeverage(uint256)`` (0x79575b23) or ``issueSessionKey`` (0x7873af1d).
    """
    bob = Bob(budget_usdc=10.0, constitution_rules=DEFAULT_BOB_RULES)
    bob.bootstrap()
    intent = bob.decide(
        alice_url="",
        market_state="long ETH after CPI print",
        transport=small_alice.client,
        chain_id=small_alice.chain_id,
        asset_address=small_alice.usdc_address,
        max_amount_usdc=small_alice.price_usdc,
        trade_size_usdc=5.0,
    )

    # Real transfer selector present.
    assert intent.selector == ERC20_TRANSFER_SELECTOR
    assert intent.calldata[:4].hex() == "a9059cbb"
    # Made-up selectors must NOT appear in the inner data.
    assert b"\x79\x57\x5b\x23" not in intent.calldata  # setLeverage
    assert b"\x78\x73\xaf\x1d" not in intent.calldata  # issueSessionKey
    # Outer wrapper uses the standard execute() selector.
    assert intent.execute_calldata[:4] == EXECUTE_SELECTOR

    # Decoding the inner calldata as transfer(address,uint256) must work.
    from eth_abi import decode as abi_decode

    decoded = abi_decode(["address", "uint256"], intent.calldata[4:])
    assert decoded[0].startswith("0x")
    # Amount in base units (USDC has 6 decimals) — 5 USDC = 5_000_000.
    assert decoded[1] == 5_000_000


# ---------------------------------------------------------------------------
# Additional safety checks — these aren't in the brief but catch the kind
# of integration bug that already bit us twice in Phase 1.5.
# ---------------------------------------------------------------------------


def test_alice_warm_boot_skips_embedding(tmp_path):
    """Second bootstrap with the same mem_path must NOT re-embed."""
    p = str(tmp_path / "alice.mem")
    a1 = Alice(corpus_size=40, mem_path=p, seed=42)
    a1.bootstrap()
    t0 = time.time()
    a2 = Alice(corpus_size=40, mem_path=p, seed=42)
    a2.bootstrap()
    cold_to_warm = time.time() - t0
    # Warm boot should be << cold boot. We're generous: under 5s.
    assert cold_to_warm < 5.0
    assert len(a1.memory) == len(a2.memory)


def test_orchestrator_step_invalid_raises():
    a = Alice(corpus_size=20, mem_path="/tmp/_orch_invalid.mem", seed=99)
    a.bootstrap()
    bob = Bob(budget_usdc=1.0, constitution_rules=DEFAULT_BOB_RULES)
    bob.bootstrap()
    orch = Orchestrator(a, bob)
    with pytest.raises(ValueError):
        orch.run_demo_step(0)
    with pytest.raises(ValueError):
        orch.run_demo_step(7)


def test_orchestrator_step_4_intent_classifies_correctly(small_alice: Alice):
    """Slice 5D-handoff intent must be tagged with the rule it should trip."""
    bob = Bob(budget_usdc=10.0, constitution_rules=DEFAULT_BOB_RULES)
    bob.bootstrap()
    orch = Orchestrator(small_alice, bob)
    orch.run_demo_step(1)
    orch.run_demo_step(2)
    r4 = orch.run_demo_step(4)
    assert r4["ok"] is True
    assert r4["evidence"]["intent_kind"] == "MAX_TRADE_SIZE"
    assert orch.last_intent is not None
    assert orch.last_intent.kind == "MAX_TRADE_SIZE"


# ---------------------------------------------------------------------------
# Bug 2 regression: Bob must default to MiniLM so his queries land in
# Alice's embedding space and the top hit is a templated trade trace, NOT
# a pinned constitution rule.
# ---------------------------------------------------------------------------


def test_bob_defaults_to_minilm_and_retrieves_templated_trace(small_alice: Alice):
    """With the default embedder Bob's top result must be a templated trace.

    Regression for the Slice 5A known gap: previously ``Bob.embedding_model``
    defaulted to ``None``, which made ``_embed`` fall back to
    ``hash_to_vec`` — a different vector space from Alice's MiniLM corpus.
    Bob's top hit became a pinned rule (hash-derived vec) rather than a
    real trade-reasoning entry. The fix defaults the embedder to MiniLM.

    Test discipline (per the brief): REAL strings, REAL MiniLM embeddings on
    both sides, REAL x402 round-trip through Slice 4. The top hit must be a
    ``t#####``-prefixed templated trace whose text matches the side/token
    we queried — not a ``pinned:`` rule.
    """
    bob = Bob(budget_usdc=10.0, constitution_rules=DEFAULT_BOB_RULES)
    # Sanity: Bob's default really is MiniLM now.
    assert bob.embedding_model == "sentence-transformers/all-MiniLM-L6-v2"
    bob.bootstrap()

    # Query Alice with a real, semantically-loaded prompt. Alice's corpus
    # contains entries templated as
    #   "{side} {token} on {venue} size {N} USDC because {signal} ..."
    # so a prompt mentioning "long ETH because funding flipped" should pull
    # back a "long ..." trade trace, not a constitution-rule pinned slot.
    results = bob.query_alice(
        alice_url="",
        market_state="long ETH because funding flipped negative on Hyperliquid",
        k=5,
        chain_id=small_alice.chain_id,
        asset_address=small_alice.usdc_address,
        max_amount_usdc=small_alice.price_usdc,
        transport=small_alice.client,
    )
    assert results, "dark pool returned no results"

    top = results[0]
    top_tid = top["trace_id"]
    top_text = (top.get("payload") or {}).get("text", "")

    # The top hit must be a templated trade-reasoning trace
    # (``t#####`` ids), not a ``pinned:`` rule.
    assert top_tid.startswith("t"), (
        f"top result was {top_tid!r} (text={top_text!r}); "
        "expected a templated trace, not a pinned rule — "
        "indicates Bob's embedder is not in Alice's space"
    )
    assert not top_tid.startswith("pinned:"), (
        f"top result is pinned slot {top_tid!r}; Bob's embedder is wrong"
    )

    # Top hit's text must look like a real trade trace, not a constitution
    # canonical string (which starts with "constitution rule ...").
    assert top_text, "top result has no payload text"
    assert not top_text.startswith("constitution rule "), (
        f"top result is a constitution rule text {top_text!r}; "
        "embedder is in the wrong space"
    )


def test_bob_hash_fallback_opt_out_still_works():
    """Tests that don't want the MiniLM dependency can pass empty string.

    The opt-out path must use ``hash_to_vec`` — deterministic, no model
    download required. Verified by computing the expected vector directly.
    """
    from agents.memory_service import hash_to_vec

    bob = Bob(
        budget_usdc=1.0,
        constitution_rules=DEFAULT_BOB_RULES,
        embedding_model="",  # opt-out sentinel
    )
    bob.bootstrap()
    vec = bob._embed("any market state")
    expected = hash_to_vec("any market state", dim=bob.embedding_dim, seed=bob.seed)
    assert np.allclose(vec, expected)

    # Also confirm the explicit "hash" sentinel.
    bob2 = Bob(
        budget_usdc=1.0,
        constitution_rules=DEFAULT_BOB_RULES,
        embedding_model="hash",
    )
    bob2.bootstrap()
    vec2 = bob2._embed("another state")
    expected2 = hash_to_vec("another state", dim=bob2.embedding_dim, seed=bob2.seed)
    assert np.allclose(vec2, expected2)
