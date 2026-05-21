"""seed_alice — produce ``/tmp/alice.mem`` for the demo.

Slice 5A's canonical seeder.  Generates Alice's 5,000-entry templated
trade-reasoning corpus with REAL ``sentence-transformers/all-MiniLM-L6-v2``
embeddings + pins three constitution rules, then saves to
``/tmp/alice.mem`` via ``MemoryService.save()``.

Idempotent:
  * if ``/tmp/alice.mem`` already exists with the right entry count,
    we leave it alone (use ``--force`` to rebuild)
  * if it's missing or undersized, we rebuild cold

F7 hardening: this seeder explicitly computes the corpus embedding mean
and pins it as ``MemoryService._centroid`` via ``set_centroid()`` BEFORE
inserting any entry. That makes the resulting ``pinned_merkle_root()``
independent of the order in which entries are written, so the on-chain
anchor is stable across boots.

This script is what ``Alice.bootstrap()`` calls into under the hood; the CLI
entrypoint is here so a hackathon operator can pre-seed once (slow first
boot, ~30s on a laptop) and have the demo warm-start in <1s afterwards.

Usage::

    python -m agents.seed_alice                # 5000 entries → /tmp/alice.mem
    python -m agents.seed_alice --n 200        # smaller corpus for fast tests
    python -m agents.seed_alice --force        # rebuild even if cache valid
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from pathlib import Path

import numpy as np

# ``agents.dark_pool`` used to evaluate a module-level FastAPI app at import
# time that loaded ``/tmp/alice.mem``, which forced this module to pre-touch a
# placeholder before importing ``agents.alice``. As of Bug 1's fix that app
# is built lazily via ``dark_pool.__getattr__``, so there's nothing to
# pre-seed and the import below is side-effect-free.

from agents.alice import (
    Alice,
    DEFAULT_EMBED_DIM,
    DEFAULT_EMBED_MODEL,
    DEFAULT_MEM_PATH,
    DEFAULT_PINNED_RULES,
    make_corpus,
)
from agents.memory_service import MemoryService, hash_to_vec

log = logging.getLogger(__name__)


def _cold_build_with_centroid(
    out_path: str,
    n: int,
    embedding_model: str,
    embedding_dim: int,
    seed: int,
    pinned_rules,
) -> None:
    """Cold-build Alice's memory with an explicit corpus-mean centroid.

    This mirrors ``Alice._build_memory`` step-for-step but:
      1. embeds the corpus FIRST
      2. computes the per-axis mean of the embeddings
      3. constructs the ``MemoryService`` and calls ``set_centroid(mean)``
         BEFORE any ``add()``

    The result is an order-independent encoding and a stable
    ``pinned_merkle_root()``. The on-disk file is the v2 npz format.
    """
    log.info(
        "seed_alice: cold build with centroid — n=%d model=%s dim=%d",
        n, embedding_model, embedding_dim,
    )
    corpus = make_corpus(n, seed=seed)

    # Lazy import — sentence_transformers drags torch with it (slow).
    from sentence_transformers import SentenceTransformer  # noqa: WPS433

    t0 = time.time()
    model = SentenceTransformer(embedding_model)
    log.info("seed_alice: model loaded in %.1fs", time.time() - t0)

    t0 = time.time()
    embs = model.encode(
        [text for _, text in corpus],
        batch_size=128,
        normalize_embeddings=True,
        show_progress_bar=False,
    ).astype(np.float32)
    log.info(
        "seed_alice: embedded %d texts in %.1fs (shape=%s)",
        n, time.time() - t0, embs.shape,
    )
    if embs.shape[1] != embedding_dim:
        raise RuntimeError(
            f"embedding dim mismatch: got {embs.shape[1]}, "
            f"expected {embedding_dim}"
        )

    # F7: explicit corpus-mean centroid. Pinned-rule vectors are computed
    # by hash_to_vec and live in roughly the same scale as the embeddings,
    # so the corpus mean is a reasonable origin for the whole memory.
    centroid = embs.mean(axis=0).astype(np.float32)
    log.info(
        "seed_alice: centroid stats — l2=%.4f mean=%.4e std=%.4e",
        float(np.linalg.norm(centroid)),
        float(centroid.mean()),
        float(centroid.std()),
    )

    mem = MemoryService(dim=embedding_dim, seed=seed)
    mem.set_centroid(centroid)

    t0 = time.time()
    for (tid, text), emb in zip(corpus, embs):
        mem.add(
            trace_id=tid,
            vec=emb,
            kind="working",
            payload={"text": text},
        )
    log.info("seed_alice: inserted %d entries in %.2fs", n, time.time() - t0)

    # Pin constitution rules — same hash_to_vec recipe Alice uses, so any
    # warm-load by ``Alice.bootstrap`` produces an identical Merkle root.
    for rule in pinned_rules:
        vec = hash_to_vec(
            rule.canonical_text(), dim=embedding_dim, seed=seed
        )
        mem.add(
            trace_id=f"pinned:{rule.rule_id}",
            vec=vec,
            kind="pinned",
            pinned=True,
            payload={
                "rule_id": rule.rule_id,
                "kind": rule.kind,
                "params": rule.params,
                "text": rule.canonical_text(),
            },
        )

    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    mem.save(out_path)
    log.info("seed_alice: saved → %s", out_path)


def seed_alice(
    out_path: str = DEFAULT_MEM_PATH,
    n: int = 5000,
    *,
    force: bool = False,
    embedding_model: str = DEFAULT_EMBED_MODEL,
    embedding_dim: int = DEFAULT_EMBED_DIM,
    seed: int = 2026,
) -> str:
    """Build / reuse the cache at ``out_path``. Returns ``out_path``.

    If a usable cache already exists (file present and ≥ expected entry
    count), reuse it. Otherwise cold-build via
    ``_cold_build_with_centroid`` and then warm-load through
    ``Alice.bootstrap()`` so the rest of the Alice setup (account, dark
    pool, etc.) still runs through the canonical path.
    """
    cache_ok = (not force) and Path(out_path).exists()
    expected_min = n + len(DEFAULT_PINNED_RULES)
    if cache_ok:
        try:
            cached = MemoryService.load(out_path)
            if len(cached) < expected_min:
                log.info(
                    "seed_alice: cache has %d entries < expected %d → rebuilding",
                    len(cached), expected_min,
                )
                cache_ok = False
        except Exception as exc:
            log.info("seed_alice: cache unreadable (%s) → rebuilding", exc)
            cache_ok = False

    if not cache_ok:
        _cold_build_with_centroid(
            out_path=out_path,
            n=n,
            embedding_model=embedding_model,
            embedding_dim=embedding_dim,
            seed=seed,
            pinned_rules=DEFAULT_PINNED_RULES,
        )

    # Now hand off to Alice.bootstrap — it will warm-load the file we just
    # produced (or the pre-existing one) and wire up the account / dark
    # pool. force_rebuild stays False because we've already done any
    # required rebuild above with the centroid invariant baked in.
    alice = Alice(
        corpus_size=n,
        embedding_model=embedding_model,
        embedding_dim=embedding_dim,
        mem_path=out_path,
        seed=seed,
        pinned_rules=DEFAULT_PINNED_RULES,
    )
    alice.bootstrap(force_rebuild=False)
    return out_path


def _cli() -> int:
    p = argparse.ArgumentParser(description="Seed Alice's MemoryService.")
    p.add_argument("--out", default=DEFAULT_MEM_PATH,
                   help="Output path for the saved memory.")
    p.add_argument("--n", type=int, default=5000,
                   help="Corpus size (working-memory entries).")
    p.add_argument("--force", action="store_true",
                   help="Rebuild even if a valid cache already exists.")
    p.add_argument("--model", default=DEFAULT_EMBED_MODEL)
    p.add_argument("--seed", type=int, default=2026)
    p.add_argument("--verbose", "-v", action="store_true")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(message)s",
    )

    t0 = time.time()
    path = seed_alice(
        out_path=args.out,
        n=args.n,
        force=args.force,
        embedding_model=args.model,
        seed=args.seed,
    )
    dt = time.time() - t0

    # Print a short summary so CI / humans can grep success.
    mem = MemoryService.load(path)
    pinned = mem.pinned_ids()
    print(
        f"seed_alice: path={path} entries={len(mem)} "
        f"pinned={len(pinned)} elapsed={dt:.2f}s"
    )
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
