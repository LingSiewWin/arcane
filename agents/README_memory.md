# MemoryService — Slice 1

RaBitQ-backed semantic memory with biological decay and a constitution-pinned slot. Owned by Slice 1 of the AgoraHack "Constrained Cognition" agent system.

## What it is

- **Storage:** 1-bit RaBitQ binary index (same math as `bench/bench_rabitq.py`) plus an FP32 unit vector kept alongside for rerank. ~50 bytes for the binary code + `4 * dim` bytes for the FP32 reranker per entry.
- **Decay:** exponential per-kind half-life. Default lambdas are `1/24h` (working), `1/7d` (episodic), `1/90d` (semantic). Entries fall off when weight < `0.05`. Pinned entries never decay, never evict.
- **Pinned slot:** holds the agent's constitution (rules). `pinned_merkle_root()` returns a deterministic SHA-256 Merkle root over pinned entries (sorted by `trace_id`); this is what gets anchored on chain by `MemoryAnchor.sol` (Slice 2).

## Quick usage

```python
import time
import numpy as np
from agents.memory_service import MemoryService, hash_to_vec

mem = MemoryService(
    dim=384,
    decay_lambdas={
        "working":  1/86400,
        "episodic": 1/(7*86400),
        "semantic": 1/(90*86400),
    },
    seed=0,  # determines the rotation matrix; commit the seed for the reveal phase
)

# Working memory — a reasoning trace embedding.
mem.add(trace_id="t1", vec=np.random.randn(384).astype(np.float32),
        kind="working", payload={"text": "thought about a 3x leverage trade"})

# Pinned: constitution rule. hash_to_vec gives a deterministic vector from a string id.
mem.add(trace_id="rule_no_leverage_above_2x",
        vec=hash_to_vec("rule_no_leverage_above_2x"),
        kind="pinned", pinned=True,
        payload={"kind": "MAX_LEVERAGE", "value": 2})

# Query (weighted nearest-neighbor over all four kinds).
results = mem.query(vec=np.random.randn(384).astype(np.float32), k=10)
# -> [("t1", 0.42), ("rule_no_leverage_above_2x", 0.31), ...]

# Tick decay (idempotent within the same tick).
mem.decay_step(now=time.time())

# Get the Merkle root for on-chain anchoring (32 bytes).
root = mem.pinned_merkle_root()

# Persist + reload (preserves the rotation seed so the root stays identical).
mem.save("/tmp/alice.mem")
mem2 = MemoryService.load("/tmp/alice.mem")
```

## Interface (frozen for Slice 5)

| Method | Returns | Notes |
|---|---|---|
| `MemoryService(dim, decay_lambdas=None, seed=0)` | instance | Default lambdas match spec §7. |
| `add(trace_id, vec, kind="working", pinned=False, payload=None)` | `None` | Re-adding the same `trace_id` overwrites. Setting `kind="pinned"` implies `pinned=True` and vice-versa. |
| `query(vec, k=10)` | `list[tuple[str, float]]` | Scores include the decay weight. Union of all kinds. |
| `decay_step(now=None)` | `None` | Idempotent per tick. Evicts non-pinned entries with weight < 0.05. |
| `pinned_merkle_root()` | `bytes` (length 32) | Deterministic; SHA-256 binary Merkle, sorted by `trace_id`. Empty pinned set returns `sha256(b"")`. |
| `save(path)` / `load(path)` | `None` / `MemoryService` | Pickle. Includes rotation seed. |
| `pinned_ids()` | `list[str]` | Sorted, for inspection. |
| `weight_of(trace_id)` | `float` | For inspection / Slice 5 diagnostics. |
| `__len__()` | `int` | Total entries (pinned + non-pinned). |

## Why RaBitQ + FP32 rerank

Per `bench/RESULTS.md`:

- 1-bit RaBitQ alone hits **65.7% recall@10** at **8.2 ms p50** and **50 B/vec** (30× cheaper than FP32 flat).
- Adding the FP32 rerank pass over the top-K binary candidates lifts recall to ~95% with a small latency cost.

On the random-gaussian test data inside `test_rerank_improves_recall`, the rerank pass lifts recall@10 from ~21% to ~59% (+38 pts). The absolute number is lower than the bench because random gaussians don't cluster — for real text embeddings the lift lands closer to the bench numbers.

## Pinned Merkle leaf format

```
leaf = sha256(
    trace_id.encode("utf-8")     ||
    b"\x00"                      ||
    bytes(bits_packed)           ||
    b"\x00"                      ||
    canonical_json(payload).encode("utf-8")
)
```

- `canonical_json` = `json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))`.
- Tree: binary, Bitcoin-style duplicate-last on odd-sized layers.
- `bits_packed` is dependent on the rotation matrix `P`, which is dependent on `seed`. **Commit the seed alongside the root** if you need an external party to recompute the leaf hashes from scratch.

## Install

```bash
uv venv --python 3.12 agents/.venv-memory
source agents/.venv-memory/bin/activate
uv pip install -r agents/requirements-memory.txt
```

## Run tests

```bash
source agents/.venv-memory/bin/activate
pytest agents/tests -v
```

All 9 tests pass (7 spec-mandated + 2 sanity).

## What this slice does NOT do

- **No FastAPI wrapper.** Slice 5 (`agents/alice.py`) wraps this class in HTTP. The class itself is sync, in-memory, single-process.
- **No embedding model.** Callers pass FP32 numpy vectors. `hash_to_vec(s)` is a string→vector convenience for the rule-pinning flow, NOT a real text embedder.
- **No on-chain interaction.** Slice 2 owns `MemoryAnchor.sol` and pulls the root via `pinned_merkle_root()`.
- **Centroid is the first-inserted vector.** The bench reference uses the full dataset mean; we don't have it at insert time. This is fine at hackathon scale but a known approximation.
- **Thread safety.** None. If Slice 5 multiplexes queries, add a lock at the HTTP layer.
