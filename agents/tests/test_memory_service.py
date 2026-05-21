"""Tests for the MemoryService (Slice 1).

Run from the repo root:

    source agents/.venv-memory/bin/activate
    pytest agents/tests -v

The seven tests below are mandated by the slice spec. No test mocks the core
algorithm — every assertion runs the real RaBitQ encode + binary scan + FP32
rerank on real numpy data.
"""
from __future__ import annotations

import os
import tempfile

import numpy as np
import pytest

from agents.memory_service import (
    THETA,
    MemoryService,
    hash_to_vec,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _rand_vecs(n: int, dim: int, seed: int = 0) -> np.ndarray:
    """Random unit-norm vectors. We L2-normalize so cosine ≈ dot and recall
    numbers are stable across seeds."""
    rng = np.random.default_rng(seed)
    v = rng.standard_normal((n, dim)).astype(np.float32)
    v /= np.linalg.norm(v, axis=1, keepdims=True)
    return v


def _ground_truth_topk(vectors: np.ndarray, q: np.ndarray, k: int) -> list[int]:
    """Brute-force FP32 cosine top-k indices for a single query."""
    q = q / max(float(np.linalg.norm(q)), 1e-12)
    # vectors are unit-norm by construction in these tests
    sims = vectors @ q
    return list(np.argsort(-sims)[:k])


# ---------------------------------------------------------------------------
# Tests required by the slice spec
# ---------------------------------------------------------------------------


def test_add_query_basic():
    """add 100 random vecs, query one of them, top-1 must be that exact entry."""
    dim = 384
    vecs = _rand_vecs(100, dim, seed=42)
    mem = MemoryService(dim=dim)
    for i, v in enumerate(vecs):
        mem.add(trace_id=f"t{i:03d}", vec=v, kind="working")
    # Query with vec #37; it must come back as top-1.
    results = mem.query(vec=vecs[37], k=5)
    assert len(results) == 5
    assert results[0][0] == "t037", f"top-1 should be self, got {results[0]}"
    # Score should be ≈ 1.0 (cosine with self).
    assert results[0][1] > 0.95, f"self-score should be near 1.0, got {results[0][1]}"


def test_decay_reduces_score():
    """A non-pinned entry's weight must drop strictly after decay_step with
    elapsed time.

    We deliberately pick λ and Δt so the post-decay weight stays *above*
    THETA — otherwise the entry would be evicted (a separate test covers
    that case). exp(-0.1) ≈ 0.905, well above 0.05.
    """
    dim = 64
    mem = MemoryService(
        dim=dim,
        decay_lambdas={"working": 0.01, "episodic": 0.01, "semantic": 0.01},
    )
    vecs = _rand_vecs(5, dim, seed=1)
    for i, v in enumerate(vecs):
        mem.add(trace_id=f"t{i}", vec=v, kind="working")
    # Force Δt = 10s; with λ=0.01, weight → exp(-0.1) ≈ 0.905.
    for e in mem.entries.values():
        e.last_decay_ts -= 10.0
    initial_w = mem.weight_of("t0")
    mem.decay_step()
    assert "t0" in mem.entries, "entry should not be evicted at this decay rate"
    new_w = mem.weight_of("t0")
    assert new_w < initial_w, f"weight should drop after decay, {initial_w} → {new_w}"
    assert new_w > THETA, f"weight should still be above eviction floor, got {new_w}"


def test_pinned_immune_to_decay():
    """Pinned entries must keep weight 1.0 after many decay ticks, even with
    aggressive lambdas."""
    dim = 64
    mem = MemoryService(
        dim=dim,
        decay_lambdas={"working": 10.0, "episodic": 10.0, "semantic": 10.0},
    )
    v = _rand_vecs(1, dim, seed=7)[0]
    mem.add(trace_id="rule_no_leverage", vec=v, kind="pinned", pinned=True)
    # Push timestamps far in the past and tick 1000 times.
    for e in mem.entries.values():
        e.last_decay_ts -= 1_000_000.0
    for _ in range(1000):
        mem.decay_step()
    assert "rule_no_leverage" in mem.entries, "pinned entry got evicted"
    assert mem.weight_of("rule_no_leverage") == 1.0, (
        f"pinned weight should stay 1.0, got {mem.weight_of('rule_no_leverage')}"
    )


def test_pinned_merkle_deterministic():
    """Two calls to pinned_merkle_root() with no intervening change must
    return the same bytes. Also: re-creating an identical service yields the
    same root (the rotation seed makes encoding reproducible).
    """
    dim = 384
    mem = MemoryService(dim=dim, seed=123)
    rules = [
        ("rule_no_leverage_above_2x", {"kind": "MAX_LEVERAGE", "value": 2}),
        ("rule_max_trade_size_1usdc", {"kind": "MAX_TRADE_SIZE", "value": 1.0}),
        ("rule_venue_blacklist", {"kind": "VENUE_BLACKLIST", "venues": ["evil_dex"]}),
    ]
    for rid, payload in rules:
        v = hash_to_vec(rid, dim=dim)
        mem.add(trace_id=rid, vec=v, kind="pinned", pinned=True, payload=payload)
    r1 = mem.pinned_merkle_root()
    r2 = mem.pinned_merkle_root()
    assert r1 == r2, "Merkle root must be stable across calls"
    assert len(r1) == 32, f"root must be 32 bytes, got {len(r1)}"

    # Reconstruct from scratch — should match because seed + payloads + ids
    # are all deterministic.
    mem2 = MemoryService(dim=dim, seed=123)
    for rid, payload in rules:
        v = hash_to_vec(rid, dim=dim)
        mem2.add(trace_id=rid, vec=v, kind="pinned", pinned=True, payload=payload)
    assert mem2.pinned_merkle_root() == r1, (
        "Root should be reproducible from same seed + same inputs"
    )


def test_pinned_merkle_changes_on_pin_change():
    """Adding a new pinned entry must change the root. Same for changing a
    payload."""
    dim = 64
    mem = MemoryService(dim=dim, seed=0)
    v = hash_to_vec("r1", dim=dim)
    mem.add(trace_id="r1", vec=v, kind="pinned", pinned=True, payload={"k": 1})
    r_before = mem.pinned_merkle_root()

    v2 = hash_to_vec("r2", dim=dim)
    mem.add(trace_id="r2", vec=v2, kind="pinned", pinned=True, payload={"k": 2})
    r_after_add = mem.pinned_merkle_root()
    assert r_after_add != r_before, "root must change after a new pinned add"

    # Changing payload (re-add same trace_id with different payload) also
    # changes the root.
    mem.add(trace_id="r1", vec=v, kind="pinned", pinned=True, payload={"k": 999})
    r_after_modify = mem.pinned_merkle_root()
    assert r_after_modify != r_after_add, "root must change after a pinned payload edit"


def test_save_load_roundtrip():
    """Save → load → identical query results, identical Merkle root."""
    dim = 128
    mem = MemoryService(dim=dim, seed=42)
    vecs = _rand_vecs(50, dim, seed=99)
    for i, v in enumerate(vecs):
        kind = "pinned" if i < 3 else "working"
        pinned = i < 3
        payload = {"i": i, "txt": f"trace-{i}"}
        mem.add(trace_id=f"t{i:03d}", vec=v, kind=kind, pinned=pinned, payload=payload)

    q = vecs[10]
    pre_results = mem.query(vec=q, k=10)
    pre_root = mem.pinned_merkle_root()

    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "snap.mem")
        mem.save(path)
        mem2 = MemoryService.load(path)

    post_results = mem2.query(vec=q, k=10)
    post_root = mem2.pinned_merkle_root()

    assert pre_root == post_root, "Merkle root should survive save/load"
    assert [r[0] for r in pre_results] == [r[0] for r in post_results], (
        "Query result order should survive save/load"
    )
    # Scores should match to float precision.
    for (id_a, s_a), (id_b, s_b) in zip(pre_results, post_results):
        assert id_a == id_b
        assert abs(s_a - s_b) < 1e-5, f"score mismatch {s_a} vs {s_b}"


def test_rerank_improves_recall():
    """The FP32 rerank pass must lift recall@10 by ≥20 pts vs the raw binary
    scan on random data.

    We measure recall against brute-force FP32 ground truth. The "binary
    only" baseline is computed inline so we don't have to expose rerank as a
    public toggle (the service always reranks; this test does its own raw
    binary calc for the comparison).
    """
    dim = 384
    n = 500
    k = 10
    vectors = _rand_vecs(n, dim, seed=2026)
    queries = _rand_vecs(50, dim, seed=5)

    mem = MemoryService(dim=dim, seed=0)
    for i, v in enumerate(vectors):
        mem.add(trace_id=f"v{i:04d}", vec=v, kind="working")

    # --- With rerank (the production path) ---
    recall_rerank = 0.0
    for q in queries:
        gt = set(_ground_truth_topk(vectors, q, k))
        pred_ids = [tid for tid, _ in mem.query(vec=q, k=k)]
        pred = {int(tid[1:]) for tid in pred_ids}  # strip "v" prefix
        recall_rerank += len(gt & pred) / k
    recall_rerank /= len(queries)

    # --- Raw binary baseline: reimplement the binary scan without rerank ---
    # We poke into the entries to score; this mirrors the bench impl exactly.
    from agents.memory_service import _SIGNS_MASK  # type: ignore

    ids = list(mem.entries.keys())
    bits_matrix = np.stack([mem.entries[i].bits_packed for i in ids])
    n_bytes = bits_matrix.shape[1]
    d_padded = n_bytes * 8

    recall_binary = 0.0
    for q in queries:
        q_unit_rot, _, _ = mem._encode(q)
        r_q_padded = np.zeros(d_padded, dtype=np.float32)
        r_q_padded[: dim] = q_unit_rot
        r_q_reshaped = r_q_padded.reshape(n_bytes, 8)
        lookup = r_q_reshaped @ _SIGNS_MASK.T
        contribs = lookup[np.arange(n_bytes), bits_matrix]
        approx = contribs.sum(axis=1)
        top_idx = np.argsort(-approx)[:k]
        pred = {int(ids[i][1:]) for i in top_idx}
        gt = set(_ground_truth_topk(vectors, q, k))
        recall_binary += len(gt & pred) / k
    recall_binary /= len(queries)

    print(
        f"\n[rerank test] binary_only={recall_binary:.3f}  "
        f"with_rerank={recall_rerank:.3f}  lift={recall_rerank - recall_binary:.3f}"
    )
    assert recall_rerank - recall_binary >= 0.20, (
        f"FP32 rerank must lift recall@10 by ≥0.20; "
        f"got binary={recall_binary:.3f}, rerank={recall_rerank:.3f}"
    )


# ---------------------------------------------------------------------------
# Extra sanity (not spec-mandated, but worth having)
# ---------------------------------------------------------------------------


def test_decay_evicts_below_theta():
    """Non-pinned entries below the eviction threshold disappear; pinned ones
    don't."""
    dim = 32
    mem = MemoryService(
        dim=dim,
        decay_lambdas={"working": 100.0, "episodic": 100.0, "semantic": 100.0},
    )
    v = _rand_vecs(2, dim, seed=11)
    mem.add(trace_id="ephemeral", vec=v[0], kind="working")
    mem.add(trace_id="rule", vec=v[1], kind="pinned", pinned=True)
    # Push 60s in the past with λ=100 → factor exp(-6000) ≈ 0
    for e in mem.entries.values():
        e.last_decay_ts -= 60.0
    mem.decay_step()
    assert "ephemeral" not in mem.entries, "should be evicted below THETA"
    assert "rule" in mem.entries, "pinned must survive"
    # Sanity: THETA is the floor; weights of survivors are ≥ THETA.
    for e in mem.entries.values():
        if not e.pinned:
            assert e.weight >= THETA


def test_empty_pinned_root_is_well_formed():
    """An empty pinned set still returns 32 bytes (so the on-chain anchor
    type stays valid)."""
    mem = MemoryService(dim=16)
    root = mem.pinned_merkle_root()
    assert isinstance(root, bytes) and len(root) == 32


# ---------------------------------------------------------------------------
# F3 — pickle removal & npz persistence hardening
# ---------------------------------------------------------------------------


def test_save_load_roundtrip_100_entries():
    """Build a memory with 100 working + 3 pinned entries, save+load, and
    assert byte-level field equality plus query-result equivalence."""
    dim = 128
    mem = MemoryService(dim=dim, seed=2026)
    vecs = _rand_vecs(103, dim, seed=7)
    for i, v in enumerate(vecs):
        if i < 3:
            mem.add(
                trace_id=f"p{i:02d}",
                vec=v,
                kind="pinned",
                pinned=True,
                payload={"rule_id": f"R{i}", "kind": "MAX_TRADE_SIZE", "i": i},
            )
        else:
            mem.add(
                trace_id=f"w{i:03d}",
                vec=v,
                kind="working",
                payload={"text": f"trace-{i}", "i": i},
            )

    q = vecs[42]
    pre_results = mem.query(vec=q, k=10)
    pre_root = mem.pinned_merkle_root()

    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "snap.mem.npz")
        mem.save(path)
        mem2 = MemoryService.load(path)

    assert len(mem) == len(mem2) == 103
    assert mem.pinned_ids() == mem2.pinned_ids()
    assert pre_root == mem2.pinned_merkle_root()

    # Per-entry equality on every persisted field.
    for tid, e in mem.entries.items():
        e2 = mem2.entries[tid]
        assert e.kind == e2.kind
        assert e.pinned == e2.pinned
        assert e.payload == e2.payload
        assert abs(e.weight - e2.weight) < 1e-6
        assert abs(e.last_decay_ts - e2.last_decay_ts) < 1e-6
        assert abs(e.norm - e2.norm) < 1e-5
        assert np.array_equal(e.bits_packed, e2.bits_packed)
        assert np.allclose(e.unit_rot, e2.unit_rot, atol=1e-6)

    # Query equivalence.
    post_results = mem2.query(vec=q, k=10)
    assert [r[0] for r in pre_results] == [r[0] for r in post_results]
    for (id_a, s_a), (id_b, s_b) in zip(pre_results, post_results):
        assert id_a == id_b
        assert abs(s_a - s_b) < 1e-5


def test_save_load_rejects_pickle_magic():
    """A file beginning with pickle protocol-2+ magic must raise ValueError
    before numpy ever touches the bytes."""
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "evil.mem")
        # A real pickle-protocol-2 stream of `{"x": 1}` would start with
        # b"\x80\x02". We only need the magic byte to trigger the check; the
        # rest is filler so the file isn't empty.
        with open(path, "wb") as f:
            f.write(b"\x80\x04evil-payload-pretend-this-is-pickle")
        with pytest.raises(ValueError, match="pickle"):
            MemoryService.load(path)


def test_save_load_rejects_malformed_metadata():
    """An npz with a `meta` array that isn't valid UTF-8 JSON must raise
    ValueError, not silently return a half-broken service."""
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "broken.mem.npz")
        # Hand-craft an npz that has the required keys but garbage in `meta`.
        meta_garbage = np.frombuffer(b"\xff\xfe-not-json-", dtype=np.uint8)
        np.savez_compressed(
            open(path, "wb"),
            vectors=np.zeros((0, 8), dtype=np.float32),
            bits_packed=np.zeros((0, 1), dtype=np.uint8),
            weights=np.zeros((0,), dtype=np.float32),
            last_decay_ts=np.zeros((0,), dtype=np.float64),
            norms=np.zeros((0,), dtype=np.float32),
            meta=meta_garbage,
            version=np.asarray([2], dtype=np.int64),
        )
        with pytest.raises(ValueError, match="malformed metadata"):
            MemoryService.load(path)


def test_save_output_is_not_pickle_or_exe():
    """The saved file must be a numpy npz (zip archive), not pickle or any
    executable format."""
    dim = 32
    mem = MemoryService(dim=dim, seed=0)
    v = _rand_vecs(3, dim, seed=11)
    for i, vv in enumerate(v):
        mem.add(trace_id=f"t{i}", vec=vv, kind="working", payload={"i": i})

    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "snap.mem.npz")
        mem.save(path)
        with open(path, "rb") as f:
            head = f.read(64)

    # Negative checks: not pickle, not Windows PE, not Linux ELF.
    assert head[0] != 0x80, "save() must not write pickle protocol-2+ output"
    assert not head.startswith(b"MZ"), "save() must not write a Windows PE"
    assert not head.startswith(b"\x7fELF"), "save() must not write an ELF"
    # Positive check: numpy npz is a PKZIP archive.
    assert head.startswith(b"PK\x03\x04"), (
        f"expected zip-magic 'PK\\x03\\x04' at offset 0, got {head[:4]!r}"
    )


# ---------------------------------------------------------------------------
# F7 — pinned Merkle root insertion-order independence
# ---------------------------------------------------------------------------


def test_pinned_merkle_root_insertion_order_independent():
    """Same rule set with explicit centroid must yield the same Merkle root
    regardless of insertion order."""
    dim = 64
    centroid = np.linspace(-0.5, 0.5, dim, dtype=np.float32)
    rules = [
        ("ruleA", {"kind": "MAX_TRADE_SIZE", "v": 1}),
        ("ruleB", {"kind": "VENUE_BLACKLIST", "v": ["evil"]}),
        ("ruleC", {"kind": "NO_LEVERAGE", "v": True}),
    ]

    m1 = MemoryService(dim=dim, seed=42)
    m1.set_centroid(centroid)
    for rid, payload in rules:
        v = hash_to_vec(rid, dim=dim)
        m1.add(trace_id=rid, vec=v, kind="pinned", pinned=True, payload=payload)

    m2 = MemoryService(dim=dim, seed=42)
    m2.set_centroid(centroid)
    for rid, payload in reversed(rules):
        v = hash_to_vec(rid, dim=dim)
        m2.add(trace_id=rid, vec=v, kind="pinned", pinned=True, payload=payload)

    assert m1.pinned_merkle_root() == m2.pinned_merkle_root()


def test_pinned_merkle_root_zero_centroid_fallback():
    """Without ``set_centroid``, the zero-origin fallback must still produce
    insertion-order independent roots."""
    dim = 64
    rules = [
        ("ruleA", {"v": 1}),
        ("ruleB", {"v": 2}),
        ("ruleC", {"v": 3}),
    ]

    m1 = MemoryService(dim=dim, seed=7)
    for rid, payload in rules:
        m1.add(
            trace_id=rid,
            vec=hash_to_vec(rid, dim=dim),
            kind="pinned",
            pinned=True,
            payload=payload,
        )

    m2 = MemoryService(dim=dim, seed=7)
    for rid, payload in reversed(rules):
        m2.add(
            trace_id=rid,
            vec=hash_to_vec(rid, dim=dim),
            kind="pinned",
            pinned=True,
            payload=payload,
        )

    assert m1.pinned_merkle_root() == m2.pinned_merkle_root()


def test_set_centroid_rejects_after_add():
    """Once any entry has been added, ``set_centroid`` must refuse to
    re-center (would silently corrupt every stored bit vector)."""
    dim = 32
    mem = MemoryService(dim=dim, seed=0)
    mem.add(
        trace_id="t0",
        vec=_rand_vecs(1, dim, seed=1)[0],
        kind="working",
        payload={},
    )
    with pytest.raises(RuntimeError, match="set_centroid"):
        mem.set_centroid(np.zeros(dim, dtype=np.float32))
