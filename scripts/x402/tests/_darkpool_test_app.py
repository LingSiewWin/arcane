"""Test-only ASGI module: a configurable DarkPoolServer for vitest subprocess tests.

This file is *not* part of the production runtime. It sits under
``scripts/x402/tests/`` so vitest's subprocess can do
``uvicorn scripts.x402.tests._darkpool_test_app:app`` against a server we
fully control — seeded MemoryService, fixed recipient, fixed chain id, etc.

Env vars consumed:
    DARKPOOL_TEST_RECIPIENT   payTo address       (required — Bob signs to this)
    DARKPOOL_TEST_PRICE_USDC  human USDC amount   (default "0.001")
    DARKPOOL_TEST_CHAIN_ID    int chain id        (default 5042002)
    DARKPOOL_TEST_USDC        USDC contract addr  (default Arc testnet 0x3600...)
    DARKPOOL_TEST_SEED_DIM    embedding dim       (default 384)
    DARKPOOL_TEST_SEED_COUNT  # vectors to seed   (default 5)
    DARKPOOL_NONCE_DB         sqlite nonce path   (default ":memory:" via InMemoryNonceStore)

The seeded MemoryService is deterministic: vector ``i`` is the unit vector
along axis ``i mod dim``. Tests can therefore predict which trace id will
score highest for a given query.
"""

from __future__ import annotations

import os

import numpy as np

from agents.dark_pool import DarkPoolServer
from agents.memory_service import MemoryService
from agents.nonce_store import InMemoryNonceStore
from agents.rate_limiter import RateLimiter


def _seed_memory(dim: int, count: int) -> MemoryService:
    mem = MemoryService(dim=dim)
    for i in range(count):
        v = np.zeros(dim, dtype=np.float32)
        v[i % dim] = 1.0
        mem.add(
            trace_id=f"trace_{i:03d}",
            vec=v,
            payload={"index": i, "note": f"unit-axis-{i % dim}"},
        )
    return mem


def _build() -> DarkPoolServer:
    recipient = os.environ.get("DARKPOOL_TEST_RECIPIENT")
    if not recipient:
        raise RuntimeError("DARKPOOL_TEST_RECIPIENT must be set")

    price = os.environ.get("DARKPOOL_TEST_PRICE_USDC", "0.001")
    chain_id = int(os.environ.get("DARKPOOL_TEST_CHAIN_ID", "5042002"))
    usdc = os.environ.get(
        "DARKPOOL_TEST_USDC",
        "0x3600000000000000000000000000000000000000",
    )
    dim = int(os.environ.get("DARKPOOL_TEST_SEED_DIM", "384"))
    count = int(os.environ.get("DARKPOOL_TEST_SEED_COUNT", "5"))

    mem = _seed_memory(dim, count)
    # Use in-memory nonce store so each test process starts clean and we
    # don't leave sqlite files behind.
    server = DarkPoolServer(
        memory=mem,
        price_per_query_usdc=price,
        payment_recipient=recipient,
        arc_chain_id=chain_id,
        usdc_address=usdc,
        nonce_store=InMemoryNonceStore(),
        # Bump rate limit so the burst tests don't trip 429 unexpectedly.
        rate_limiter=RateLimiter(capacity=10_000, refill_per_second=1000.0),
    )
    return server


server = _build()
app = server.app
