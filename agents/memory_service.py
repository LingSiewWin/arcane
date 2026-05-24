"""MemoryService — Slice 1 of the AgoraHack "Constrained Cognition" agent.

A RaBitQ-backed semantic memory with biological decay and a constitution-pinned slot.

Public interface (consumed by Slice 5 / agents/alice.py and bob.py):

    mem = MemoryService(dim=384, decay_lambdas={"working": 1/86400, ...})
    mem.set_centroid(corpus_mean)              # OPTIONAL — see F7 below
    mem.add(trace_id="t1", vec=v, kind="working", pinned=False, payload={...})
    results = mem.query(vec=q, k=10)            # [(trace_id, score), ...]
    mem.decay_step(now=time.time())             # idempotent per tick
    root = mem.pinned_merkle_root()             # bytes32, deterministic
    mem.save("/tmp/alice.mem"); MemoryService.load(...)

Algorithm
---------

Storage layer: 1-bit RaBitQ binary index, vendored from `bench/bench_rabitq.py`.
That module is in the (read-only) `bench/` tree, so we re-implement the same
math here verbatim — same seed semantics, same rotation matrix construction,
same packbits layout. Tests in `bench/` continue to gate the reference impl;
this module owns its own copy to keep the agents package self-contained.

Storage is genuinely 1-bit. Per entry we keep ONLY:
  * `bits_packed`  — sign(rotated) bit-packed, ceil(d/8) bytes (48 B at d=384)
  * `l1`           — the L1 norm of the rotated unit vector, one float (4 B)
  * `norm`         — pre-centering L2 norm, one float (diagnostics)
We do NOT retain the FP32 vector. At d=384 that is ~52 B/entry vs 1,536 B for
FP32 — a ~30x reduction, the whole point of the "most memory-efficient" thesis.
(The pre-F5 design kept the FP32 `unit_rot` for rerank, which made storage
*larger* than FP32 — bits + floats — and silently defeated the compression.)

Query path (RaBitQ unbiased estimator — no FP32 store, no rerank pass):
  1. Rotate the full-precision QUERY (only a handful per tick — cheap).
  2. Binary scan: approx_inner_i = <sign(o_r,i), q_r> via a byte lookup table.
  3. Calibrated cosine estimate: cos(o_i, q) ≈ approx_inner_i / l1_i.
     This is the RaBitQ unbiased estimator: for a self-query it returns exactly
     1.0 (approx_inner == l1), and it recovers recall well above raw popcount
     (which ignores the per-vector l1 calibration) — all without storing a
     single FP32 coordinate.
  4. Multiply by current decay weight; pinned entries always weight 1.0.
  5. Return top-k by weighted score.

Decay:
  weight *= exp(-λ_kind · Δt)
  evict if weight < THETA AND not pinned.

Pinned Merkle root:
  Sort pinned entries lexically by trace_id; leaf = sha256(trace_id_bytes ||
  packed_bits || canonical_json(payload)). Build a binary Merkle tree; pad
  the last odd leaf by duplicating it (Bitcoin-style). Root is 32 bytes.

Persistence (F3 hardening):
  We store a single `numpy.savez_compressed` archive — zero pickle, zero
  `allow_pickle` — with one numpy array per `_Entry` field stacked over all
  entries (bits_packed, l1, norms, weights, last_decay_ts), plus a sidecar
  `meta` array holding UTF-8 JSON bytes for the per-entry scalars (trace_ids,
  kinds, pinned flags, payloads) and the service-level config (dim, seed,
  decay_lambdas, centroid). `load()` rejects any file beginning with the pickle
  magic byte (`0x80`) before touching it. The archive carries NO FP32 vectors —
  the 1-bit codes + l1 scalars are the whole index.

Centroid policy (F7 hardening):
  The encoder centers vectors against `self._centroid` before rotation. Two
  ways to set it:

    1. Explicitly via `set_centroid(corpus_mean)` BEFORE any `add()`. The
       seed pipeline (`agents/seed_alice.py`) computes the corpus embedding
       mean and passes it here so the encoding is well-conditioned and
       insertion-order independent.

    2. If `set_centroid` is never called, the first `add()` arms the
       centroid to **zero** (the origin). This is still insertion-order
       independent (every entry centers against the same fixed origin), it
       just sacrifices a couple of points of rerank quality versus a
       corpus-mean centroid.

  The pre-F7 behaviour ("first inserted vector becomes the centroid") is
  GONE: it produced different `bits_packed` per entry depending on which
  vector arrived first, which in turn changed `pinned_merkle_root()` and
  caused the on-chain anchor to drift across boots even when the rule set
  was identical.

This is one slice; ~500 LoC. No FastAPI wrapper here — Slice 5 wraps the
class with whatever HTTP shim it needs.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import time
from dataclasses import dataclass, field
from typing import Iterable

import numpy as np

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Decay threshold: entries with weight strictly below this are evicted unless
# pinned. 0.05 from the spec (§7).
THETA: float = 0.05

# Kinds. "pinned" entries are stored in the pinned slot; the kind label on
# non-pinned entries chooses which decay constant applies.
VALID_KINDS = ("working", "episodic", "semantic", "pinned")

# v1: pickle. v2: npz with FP32 vectors retained for rerank. v3 (F5/Phase 5):
# npz WITHOUT FP32 vectors — only the 1-bit codes + l1 scalars (the genuine
# memory-efficient store). `load()` refuses pickle and refuses older versions.
_PERSIST_VERSION: int = 3

# Pickle magic byte for protocol 2 and above. Protocols 0/1 start with ASCII
# but no one writes those any more; protocol 2+ covers the practical RCE
# attack surface.
_PICKLE_MAGIC_BYTE: int = 0x80


# ---------------------------------------------------------------------------
# Entry record
# ---------------------------------------------------------------------------


@dataclass
class _Entry:
    """One memory record — a genuinely 1-bit RaBitQ code, NO FP32 vector.

    The only per-entry payload is the packed sign bits + two float scalars
    (`l1` for the estimator, `norm` for diagnostics). This is what makes the
    store ~30x smaller than FP32."""

    trace_id: str
    kind: str           # one of VALID_KINDS
    pinned: bool
    payload: dict
    # Packed sign bits of the rotated unit vector, shape (ceil(dim/8),) uint8.
    # The entire vector representation used for search (with `l1`).
    bits_packed: np.ndarray
    # L1 norm of the rotated unit vector (== Σ|o_r|). The RaBitQ estimator
    # divides the binary inner product by this to get a calibrated cosine.
    l1: float
    # Pre-centering L2 norm (diagnostics; not used in ranking).
    norm: float
    # Decay weight in [0, 1]. Always 1.0 for pinned.
    weight: float = 1.0
    # Timestamp at which `weight` was last updated. decay_step() uses this to
    # compute Δt; idempotent within the same tick because Δt collapses to 0.
    last_decay_ts: float = field(default_factory=time.time)


# ---------------------------------------------------------------------------
# Binary index helpers — vendored from bench/bench_rabitq.py
# ---------------------------------------------------------------------------


def _build_rotation(dim: int, seed: int) -> np.ndarray:
    """Random orthogonal rotation matrix. Same construction as
    `RaBitQ1BitIndex.__init__` in bench/bench_rabitq.py: QR of an iid Gaussian.

    Reproducible from `seed` alone, which is what makes `save/load` byte-
    identical across runs and what makes the commit-reveal flow in §6 of the
    design spec possible.
    """
    rng = np.random.default_rng(seed)
    G = rng.standard_normal((dim, dim)).astype(np.float32)
    Q, _ = np.linalg.qr(G)
    return Q.astype(np.float32)


def _pack_bits(signed: np.ndarray, dim: int) -> np.ndarray:
    """Pack a (d,) array of +/-1 floats into a uint8 bit vector.

    Mirrors `RaBitQ1BitIndex._pack_bits` in bench. We accept a 1-D input for
    convenience here (the bench version is batched)."""
    bits = (signed > 0).astype(np.uint8)
    if dim % 8 != 0:
        pad = 8 - (dim % 8)
        bits = np.concatenate([bits, np.zeros(pad, dtype=np.uint8)])
    return np.packbits(bits)


def _byte_lookup_table() -> np.ndarray:
    """Precomputed (256, 8) +/-1 mask: bit-pattern → per-bit sign.

    Bit ordering matches `numpy.packbits` (MSB first), same as the bench impl.
    """
    bit_masks = np.array(
        [[(p >> (7 - j)) & 1 for j in range(8)] for p in range(256)],
        dtype=np.float32,
    )
    return bit_masks * 2.0 - 1.0  # (256, 8) in {+1, -1}


_SIGNS_MASK = _byte_lookup_table()


# ---------------------------------------------------------------------------
# MemoryService
# ---------------------------------------------------------------------------


class MemoryService:
    """RaBitQ-backed memory with decay + pinned Merkle root.

    Single source of truth for the agent's reasoning trace store. Thread-safe?
    No — the assumption is one decision loop per agent process. Slice 5 owns
    the FastAPI wrapper and can add a lock there if it actually concurrently
    serves.
    """

    # ---- Construction --------------------------------------------------

    def __init__(
        self,
        dim: int,
        decay_lambdas: dict[str, float] | None = None,
        seed: int = 0,
    ):
        self.dim = int(dim)
        # Default lambdas from spec §7.
        self.decay_lambdas: dict[str, float] = decay_lambdas or {
            "working": 1.0 / 86400,           # 1/24h
            "episodic": 1.0 / (7 * 86400),    # 1/7d
            "semantic": 1.0 / (90 * 86400),   # 1/90d
        }
        self.seed = int(seed)
        self.P = _build_rotation(self.dim, self.seed)

        # Centroid policy: explicit via `set_centroid()` before any add, or
        # zero (origin) on first add as a fallback. See module docstring
        # F7 hardening section.
        self._centroid: np.ndarray | None = None

        # All entries keyed by trace_id. Pinned + non-pinned co-mingle here;
        # the .pinned bool on each entry separates them at query time.
        self.entries: dict[str, _Entry] = {}

    # ---- Centroid management (F7) --------------------------------------

    def set_centroid(self, vec: np.ndarray | None) -> None:
        """Pin the centering origin used by the encoder. Must be called
        BEFORE any ``add()`` — once any entry exists, re-centering would
        invalidate every stored bit vector and silently corrupt the Merkle
        root.

        Pass ``vec=None`` to reset (next ``add()`` will fall back to the
        zero-centroid default). Calling with a numpy array copies it as
        float32 of shape ``(dim,)``.
        """
        if len(self.entries) > 0:
            raise RuntimeError(
                "set_centroid() cannot be called after entries have been "
                "added — re-centering would invalidate stored bit vectors. "
                "Construct a fresh MemoryService instead."
            )
        if vec is None:
            self._centroid = None
            return
        c = np.asarray(vec, dtype=np.float32).reshape(-1)
        if c.shape[0] != self.dim:
            raise ValueError(
                f"centroid dim {c.shape[0]} != index dim {self.dim}"
            )
        self._centroid = c.copy()

    # ---- Encoding ------------------------------------------------------

    def _ensure_centroid(self, vec: np.ndarray) -> None:
        """Arm a fixed centroid on first use.

        If the caller never invoked ``set_centroid``, we lock in the **zero
        vector** (origin) as the centering reference. This guarantees the
        encoding — and therefore ``pinned_merkle_root()`` — is independent
        of which entry is inserted first.
        """
        if self._centroid is None:
            self._centroid = np.zeros(self.dim, dtype=np.float32)

    def _encode(
        self, vec: np.ndarray
    ) -> tuple[np.ndarray, np.ndarray, float, float]:
        """Encode a raw FP32 vector into (rotated, bits_packed, l1, norm).

        rotated:   (dim,) — centered, L2-normalized, rotated FP32. Used ONLY
                   transiently for the query (never stored per entry).
        bits_packed: (ceil(dim/8),) uint8 — sign bits of `rotated`. Stored.
        l1:        Σ|rotated| — the RaBitQ estimator's per-vector calibration.
        norm:      pre-centering scalar L2 norm (diagnostics).
        """
        v = np.asarray(vec, dtype=np.float32).reshape(-1)
        if v.shape[0] != self.dim:
            raise ValueError(f"vec dim {v.shape[0]} != index dim {self.dim}")
        self._ensure_centroid(v)
        centered = v - self._centroid
        norm = float(np.linalg.norm(centered))
        if norm == 0.0:
            # Zero-after-centering edge case: fall back to the raw vector
            # direction so the entry isn't degenerate.
            raw_norm = float(np.linalg.norm(v))
            if raw_norm == 0.0:
                unit = np.zeros_like(v)
            else:
                unit = v / raw_norm
            norm = raw_norm
        else:
            unit = centered / norm
        # Apply the random rotation (column-major like the bench impl: u @ P.T).
        rotated = (unit @ self.P.T).astype(np.float32)
        signs = np.where(rotated >= 0, 1.0, -1.0).astype(np.float32)
        bits = _pack_bits(signs, self.dim)
        l1 = float(np.abs(rotated).sum())
        return rotated, bits, l1, norm

    # ---- Public API ----------------------------------------------------

    def add(
        self,
        trace_id: str,
        vec: np.ndarray,
        kind: str = "working",
        pinned: bool = False,
        payload: dict | None = None,
    ) -> None:
        """Insert (or overwrite) a single memory entry.

        Pinned entries skip decay and contribute to `pinned_merkle_root()`.
        Re-adding the same trace_id replaces the entry — useful for promoting
        a working memory into the pinned slot.
        """
        if kind not in VALID_KINDS:
            raise ValueError(f"kind must be one of {VALID_KINDS}, got {kind!r}")
        if pinned and kind != "pinned":
            # Convention: pinned entries are tagged kind="pinned". Auto-fix
            # rather than raise to keep the call site simple.
            kind = "pinned"
        if (not pinned) and kind == "pinned":
            # Reverse case: caller passed kind="pinned" without pinned=True.
            pinned = True
        payload = payload or {}
        _rotated, bits, l1, norm = self._encode(vec)
        self.entries[str(trace_id)] = _Entry(
            trace_id=str(trace_id),
            kind=kind,
            pinned=bool(pinned),
            payload=payload,
            bits_packed=bits,
            l1=l1,
            norm=norm,
            weight=1.0,
            last_decay_ts=time.time(),
        )

    def query(self, vec: np.ndarray, k: int = 10) -> list[tuple[str, float]]:
        """Weighted nearest-neighbor search via the RaBitQ unbiased estimator.

        Returns up to k (trace_id, score) tuples sorted by descending score.
        Score = est_cosine(query, entry) * entry.weight, where

            est_cosine_i = <sign(o_r,i), q_r> / l1_i

        is the calibrated cosine estimate — the binary inner product divided by
        the per-vector L1 norm. No FP32 vectors are touched: only the stored
        1-bit codes + the (transiently rotated) full-precision query. A
        self-query returns est_cosine == 1.0.
        """
        if len(self.entries) == 0:
            return []
        q_rotated, _bits, _l1, _norm = self._encode(vec)

        # ---- Binary scan: approx_inner_i = <sign(o_r,i), q_r> -----------
        ids = list(self.entries.keys())
        bits_matrix = np.stack([self.entries[i].bits_packed for i in ids])  # (N, n_bytes)

        n_bytes = bits_matrix.shape[1]
        d_padded = n_bytes * 8
        r_q_padded = np.zeros(d_padded, dtype=np.float32)
        r_q_padded[: self.dim] = q_rotated
        r_q_reshaped = r_q_padded.reshape(n_bytes, 8)
        lookup = r_q_reshaped @ _SIGNS_MASK.T   # (n_bytes, 256)
        contribs = lookup[np.arange(n_bytes), bits_matrix]  # (N, n_bytes)
        approx_inner = contribs.sum(axis=1)     # (N,) = <q_r, sign_i>

        # ---- RaBitQ estimator: divide by per-vector l1 (calibration) ----
        l1 = np.array([self.entries[i].l1 for i in ids], dtype=np.float32)
        l1_safe = np.where(l1 > 0, l1, 1.0)
        est_cosine = approx_inner / l1_safe     # (N,) calibrated cosine, self==1.0

        weights = np.array([self.entries[i].weight for i in ids], dtype=np.float32)
        scored = est_cosine * weights

        # Final top-k (partial sort, then order the winners).
        n = len(ids)
        kk = min(k, n)
        top = np.argpartition(-scored, kk - 1)[:kk] if kk < n else np.arange(n)
        order = top[np.argsort(-scored[top])]
        return [(ids[j], float(scored[j])) for j in order]

    def decay_step(self, now: float | None = None) -> None:
        """Apply exponential decay to every non-pinned entry.

        Idempotent within the same tick: Δt = max(0, now - last_decay_ts) and
        last_decay_ts is updated, so calling twice in the same second is a
        near-no-op. Entries with weight < THETA after decay are evicted
        (pinned entries are immune to both decay and eviction).
        """
        if now is None:
            now = time.time()
        to_drop: list[str] = []
        for tid, e in self.entries.items():
            if e.pinned:
                # Pinned entries never decay. Refresh the timestamp anyway so
                # a future unpin doesn't get retroactively penalised.
                e.last_decay_ts = now
                continue
            dt = max(0.0, now - e.last_decay_ts)
            lam = self.decay_lambdas.get(e.kind, 0.0)
            e.weight *= math.exp(-lam * dt)
            e.last_decay_ts = now
            if e.weight < THETA:
                to_drop.append(tid)
        for tid in to_drop:
            del self.entries[tid]

    # ---- Pinned Merkle root --------------------------------------------

    def _pinned_leaf(self, e: _Entry) -> bytes:
        """Compute the leaf hash for one pinned entry.

        leaf = sha256(trace_id_utf8 || packed_bits || canonical_json(payload)).
        Canonical JSON = sort_keys=True, no whitespace, ensure_ascii=False.
        """
        h = hashlib.sha256()
        h.update(e.trace_id.encode("utf-8"))
        h.update(b"\x00")  # separator to avoid concat ambiguity
        h.update(bytes(e.bits_packed))
        h.update(b"\x00")
        # Canonical JSON — payload determinism is load-bearing.
        h.update(
            json.dumps(
                e.payload,
                sort_keys=True,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        return h.digest()

    def pinned_merkle_root(self) -> bytes:
        """Deterministic SHA-256 Merkle root over pinned entries.

        Returns 32 bytes. Sort order: lexicographic ascending by trace_id.
        Tree construction: binary, Bitcoin-style duplicate-last on odd levels.
        Empty pinned set returns sha256(b"") so the chain anchor is still a
        valid bytes32.
        """
        pinned = sorted(
            (e for e in self.entries.values() if e.pinned),
            key=lambda e: e.trace_id,
        )
        if not pinned:
            return hashlib.sha256(b"").digest()

        layer = [self._pinned_leaf(e) for e in pinned]
        while len(layer) > 1:
            if len(layer) % 2 == 1:
                layer.append(layer[-1])  # duplicate-last padding
            nxt: list[bytes] = []
            for i in range(0, len(layer), 2):
                nxt.append(hashlib.sha256(layer[i] + layer[i + 1]).digest())
            layer = nxt
        return layer[0]

    # ---- Persistence (F3 hardening) ------------------------------------

    def save(self, path: str) -> None:
        """Persist the service to a `numpy.savez_compressed` archive.

        Format (v2): one zip-compressed npz file containing per-entry numpy
        arrays stacked across all entries, plus a `meta` byte array carrying
        UTF-8 JSON for the per-entry scalars (trace_ids, kinds, pinned,
        payloads) and the service-level config (dim, seed, decay_lambdas,
        centroid). NO pickle. Written atomically via tmp + os.replace.
        """
        entries = list(self.entries.values())
        n_bytes = (self.dim + 7) // 8

        if entries:
            bits_packed = np.stack([e.bits_packed for e in entries]).astype(np.uint8)
            l1s = np.asarray([e.l1 for e in entries], dtype=np.float32)
            weights = np.asarray(
                [e.weight for e in entries], dtype=np.float32
            )
            last_decay_ts = np.asarray(
                [e.last_decay_ts for e in entries], dtype=np.float64
            )
            norms = np.asarray([e.norm for e in entries], dtype=np.float32)
        else:
            bits_packed = np.zeros((0, n_bytes), dtype=np.uint8)
            l1s = np.zeros((0,), dtype=np.float32)
            weights = np.zeros((0,), dtype=np.float32)
            last_decay_ts = np.zeros((0,), dtype=np.float64)
            norms = np.zeros((0,), dtype=np.float32)

        meta = {
            "version": _PERSIST_VERSION,
            "dim": self.dim,
            "seed": self.seed,
            "decay_lambdas": dict(self.decay_lambdas),
            "centroid": (
                self._centroid.tolist() if self._centroid is not None else None
            ),
            "trace_ids": [e.trace_id for e in entries],
            "kinds": [e.kind for e in entries],
            "pinned": [bool(e.pinned) for e in entries],
            "payloads": [e.payload for e in entries],
        }
        meta_bytes = json.dumps(
            meta, sort_keys=False, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        meta_arr = np.frombuffer(meta_bytes, dtype=np.uint8)
        version_arr = np.asarray([_PERSIST_VERSION], dtype=np.int64)

        tmp = path + ".tmp"
        # `np.savez_compressed` appends `.npz` if the path lacks the suffix,
        # which would silently move our file. Force the literal name by
        # passing an open file handle.
        with open(tmp, "wb") as f:
            np.savez_compressed(
                f,
                bits_packed=bits_packed,
                l1s=l1s,
                weights=weights,
                last_decay_ts=last_decay_ts,
                norms=norms,
                meta=meta_arr,
                version=version_arr,
            )
        os.replace(tmp, path)

    @classmethod
    def load(cls, path: str) -> "MemoryService":
        """Load a v2 npz archive. Refuses anything that smells like pickle."""
        # 1. Pickle-magic sniff. Pickle protocols 2+ all start with 0x80.
        with open(path, "rb") as f:
            head = f.read(1)
        if head and head[0] == _PICKLE_MAGIC_BYTE:
            raise ValueError(
                f"refusing to load pickle-magic file {path!r}; "
                "MemoryService v2 uses numpy.savez_compressed (zip format)"
            )

        # 2. Strict numpy load. `allow_pickle=False` rejects any object-dtype
        #    payload outright — the only way an attacker could sneak pickle
        #    bytes past the magic-byte check is via an object array, and we
        #    refuse to deserialise those.
        try:
            with np.load(path, allow_pickle=False) as data:
                required = {
                    "bits_packed",
                    "l1s",
                    "weights",
                    "last_decay_ts",
                    "norms",
                    "meta",
                    "version",
                }
                missing = required - set(data.files)
                if missing:
                    raise ValueError(
                        f"malformed memory archive {path!r}: "
                        f"missing keys {sorted(missing)}"
                    )

                meta_arr = data["meta"]
                try:
                    meta_text = bytes(meta_arr).decode("utf-8")
                    meta = json.loads(meta_text)
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise ValueError(
                        f"malformed metadata in {path!r}: {exc}"
                    ) from exc

                version_arr = data["version"]
                file_version = int(version_arr[0])
                meta_version = int(meta.get("version", -1))
                if file_version != meta_version:
                    raise ValueError(
                        f"version mismatch in {path!r}: "
                        f"file={file_version} meta={meta_version}"
                    )
                if file_version != _PERSIST_VERSION:
                    raise ValueError(
                        f"unsupported persist version {file_version} "
                        f"(this build expects {_PERSIST_VERSION})"
                    )

                # Pull arrays into ordinary memory so the npz can be closed.
                bits_packed = np.asarray(data["bits_packed"], dtype=np.uint8)
                l1s = np.asarray(data["l1s"], dtype=np.float32)
                weights = np.asarray(data["weights"], dtype=np.float32)
                last_decay_ts = np.asarray(
                    data["last_decay_ts"], dtype=np.float64
                )
                norms = np.asarray(data["norms"], dtype=np.float32)
        except ValueError:
            raise
        except Exception as exc:
            # numpy raises a bare-`Exception` (or `BadZipFile`) on corrupt
            # archives; normalise to ValueError so callers can catch one type.
            raise ValueError(
                f"could not read memory archive {path!r}: {exc}"
            ) from exc

        # 3. Cross-check shapes against the metadata.
        for key in ("dim", "seed", "decay_lambdas", "trace_ids", "kinds",
                    "pinned", "payloads"):
            if key not in meta:
                raise ValueError(
                    f"malformed metadata in {path!r}: missing key {key!r}"
                )
        trace_ids = list(meta["trace_ids"])
        n = len(trace_ids)
        if (
            l1s.shape[0] != n
            or bits_packed.shape[0] != n
            or weights.shape[0] != n
            or last_decay_ts.shape[0] != n
            or norms.shape[0] != n
            or len(meta["kinds"]) != n
            or len(meta["pinned"]) != n
            or len(meta["payloads"]) != n
        ):
            raise ValueError(
                f"malformed memory archive {path!r}: row-count mismatch "
                f"(n={n}, vectors={vectors.shape[0]})"
            )

        # 4. Reconstruct.
        inst = cls(
            dim=int(meta["dim"]),
            decay_lambdas={
                str(k): float(v) for k, v in meta["decay_lambdas"].items()
            },
            seed=int(meta["seed"]),
        )
        if meta.get("centroid") is not None:
            inst._centroid = np.asarray(meta["centroid"], dtype=np.float32)
        for i, tid in enumerate(trace_ids):
            inst.entries[str(tid)] = _Entry(
                trace_id=str(tid),
                kind=str(meta["kinds"][i]),
                pinned=bool(meta["pinned"][i]),
                payload=dict(meta["payloads"][i]) if meta["payloads"][i] is not None else {},
                bits_packed=bits_packed[i].astype(np.uint8).copy(),
                l1=float(l1s[i]),
                norm=float(norms[i]),
                weight=float(weights[i]),
                last_decay_ts=float(last_decay_ts[i]),
            )
        return inst

    # ---- Inspection helpers (handy for tests + Slice 5) ----------------

    def __len__(self) -> int:
        return len(self.entries)

    def pinned_ids(self) -> list[str]:
        return sorted(tid for tid, e in self.entries.items() if e.pinned)

    def weight_of(self, trace_id: str) -> float:
        return self.entries[trace_id].weight

    def memory_stats(self) -> dict:
        """Real per-entry footprint of the 1-bit store vs an FP32 baseline.

        bytes_per_vec = ceil(dim/8) packed-bit code + 4 (l1 fp32) + 4 (norm
        fp32). The FP32 baseline is dim*4. This is the measured basis for the
        'most memory-efficient' claim — not an estimate.
        """
        n_bits_bytes = (self.dim + 7) // 8
        bytes_per_vec = n_bits_bytes + 4 + 4  # code + l1 + norm scalars
        fp32_bytes_per_vec = self.dim * 4
        n = len(self.entries)
        return {
            "entries": n,
            "dim": self.dim,
            "bytes_per_vec": bytes_per_vec,
            "fp32_bytes_per_vec": fp32_bytes_per_vec,
            "compression_x": round(fp32_bytes_per_vec / bytes_per_vec, 2),
            "index_bytes": bytes_per_vec * n,
            "fp32_index_bytes": fp32_bytes_per_vec * n,
        }


# ---------------------------------------------------------------------------
# Convenience: deterministic hash → vector for constitution rule pinning
# ---------------------------------------------------------------------------


def hash_to_vec(s: str, dim: int = 384, seed: int = 0) -> np.ndarray:
    """Deterministically derive a unit FP32 vector from a string.

    Used by the bootstrap path (see spec §6) to pin constitution rules
    without a real text embedding model. Seeded by the SHA-256 of the string,
    so the same rule always maps to the same vector and the pinned Merkle
    root is reproducible across processes.
    """
    digest = hashlib.sha256(s.encode("utf-8")).digest()
    # Use first 8 bytes as a numpy RNG seed.
    rng_seed = int.from_bytes(digest[:8], "big", signed=False) ^ seed
    rng = np.random.default_rng(rng_seed)
    v = rng.standard_normal(dim).astype(np.float32)
    n = float(np.linalg.norm(v))
    if n == 0:
        return v
    return v / n
