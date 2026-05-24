"""Tests for ``agents.rate_limiter``.

The token-bucket uses ``time.monotonic()`` internally; for deterministic
refill behaviour the tests monkeypatch the ``_now`` hook so we don't have
to actually sleep.

Two surfaces are exercised:
  * ``try_consume_sync`` / ``retry_after_sync`` — the synchronous API
    used by these unit tests (deterministic monkeypatched clock).
  * ``try_consume`` / ``retry_after`` — the async API used by the
    FastAPI request handlers. The B14 concurrency test (last block)
    spawns 100 concurrent coroutines and asserts the event loop is
    never blocked (no GIL-style serialisation).
"""

from __future__ import annotations

import asyncio
import time

import pytest

from agents.rate_limiter import RateLimiter


@pytest.fixture
def fake_clock(monkeypatch):
    """Drives the limiter's ``_now`` hook from a settable ``[0]`` cell."""
    state = {"t": 1000.0}

    def fake_now(_self):
        return state["t"]

    monkeypatch.setattr(RateLimiter, "_now", fake_now)
    return state


def test_rate_limiter_blocks_after_capacity_exceeded(fake_clock):
    rl = RateLimiter(capacity=3, refill_per_second=1.0)
    # First 3 succeed.
    for _ in range(3):
        assert rl.try_consume_sync("alice") is True
    # 4th in the same instant must fail — bucket empty.
    assert rl.try_consume_sync("alice") is False


def test_rate_limiter_refills_over_time(fake_clock):
    rl = RateLimiter(capacity=2, refill_per_second=1.0)
    assert rl.try_consume_sync("bob")
    assert rl.try_consume_sync("bob")
    assert rl.try_consume_sync("bob") is False

    # Advance 1 second — should accrue exactly one token.
    fake_clock["t"] += 1.0
    assert rl.try_consume_sync("bob") is True
    # And we're empty again immediately after.
    assert rl.try_consume_sync("bob") is False


def test_rate_limiter_per_key_isolation(fake_clock):
    rl = RateLimiter(capacity=2, refill_per_second=1.0)
    assert rl.try_consume_sync("alice")
    assert rl.try_consume_sync("alice")
    assert rl.try_consume_sync("alice") is False
    # Bob is unaffected.
    assert rl.try_consume_sync("bob") is True
    assert rl.try_consume_sync("bob") is True
    assert rl.try_consume_sync("bob") is False


def test_rate_limiter_capacity_caps_refill(fake_clock):
    """Even with a huge time jump the bucket never goes above capacity."""
    rl = RateLimiter(capacity=5, refill_per_second=100.0)
    # Drain.
    for _ in range(5):
        assert rl.try_consume_sync("c")
    # Advance an hour — bucket should not exceed 5 tokens.
    fake_clock["t"] += 3600
    # 5 must succeed, the 6th must fail.
    for _ in range(5):
        assert rl.try_consume_sync("c") is True
    assert rl.try_consume_sync("c") is False


def test_rate_limiter_reset_single_key(fake_clock):
    rl = RateLimiter(capacity=1, refill_per_second=0.001)
    assert rl.try_consume_sync("alice")
    assert rl.try_consume_sync("alice") is False
    # Reset specifically Alice.
    rl.reset("alice")
    assert rl.try_consume_sync("alice") is True


def test_rate_limiter_reset_all(fake_clock):
    rl = RateLimiter(capacity=1, refill_per_second=0.001)
    assert rl.try_consume_sync("alice")
    assert rl.try_consume_sync("bob")
    assert rl.try_consume_sync("alice") is False
    assert rl.try_consume_sync("bob") is False
    rl.reset()  # reset all
    assert rl.try_consume_sync("alice")
    assert rl.try_consume_sync("bob")


def test_rate_limiter_consume_multiple_tokens(fake_clock):
    rl = RateLimiter(capacity=10, refill_per_second=1.0)
    assert rl.try_consume_sync("x", tokens=4)
    assert rl.try_consume_sync("x", tokens=4)
    # 8 consumed; only 2 left; asking for 3 must fail.
    assert rl.try_consume_sync("x", tokens=3) is False
    # Confirm bucket unchanged on failed deduction.
    assert rl.try_consume_sync("x", tokens=2) is True


def test_rate_limiter_retry_after_reports_real_seconds(fake_clock):
    rl = RateLimiter(capacity=1, refill_per_second=0.5)  # 1 token / 2s
    assert rl.try_consume_sync("x")
    # Bucket empty — retry-after should be ~2 seconds.
    ra = rl.retry_after_sync("x")
    assert 1.9 <= ra <= 2.1, ra


def test_rate_limiter_rejects_bad_args():
    with pytest.raises(ValueError):
        RateLimiter(capacity=0, refill_per_second=1.0)
    with pytest.raises(ValueError):
        RateLimiter(capacity=1, refill_per_second=0)
    rl = RateLimiter(capacity=1, refill_per_second=1.0)
    with pytest.raises(ValueError):
        rl.try_consume_sync("x", tokens=0)


def test_rate_limiter_real_clock_smoke():
    """Sanity check with the real monotonic clock — no fake."""
    rl = RateLimiter(capacity=2, refill_per_second=50.0)
    assert rl.try_consume_sync("k")
    assert rl.try_consume_sync("k")
    assert rl.try_consume_sync("k") is False
    # Sleep enough for one refill (1/50 s = 20ms — give 30ms).
    time.sleep(0.03)
    assert rl.try_consume_sync("k") is True


# ---------------------------------------------------------------------------
# Phase 5 Stream D — B14: async API must not block the event loop.
#
# The pre-fix limiter used ``threading.Lock`` even when called from inside
# an ``async def`` handler. Under contention this serialised the whole
# event loop on a kernel mutex. With ``asyncio.Lock`` the critical section
# is held only while no coroutine is awaiting, so 100 concurrent consumes
# should complete far faster than 100 × single-call cost.
# ---------------------------------------------------------------------------


def test_rate_limiter_async_basic_consume():
    """async try_consume returns True until capacity, then False."""

    async def _go():
        rl = RateLimiter(capacity=3, refill_per_second=0.001)
        results = []
        for _ in range(5):
            results.append(await rl.try_consume("alice"))
        return results

    res = asyncio.run(_go())
    assert res == [True, True, True, False, False]


def test_rate_limiter_async_retry_after_reports_seconds():
    async def _go():
        rl = RateLimiter(capacity=1, refill_per_second=0.5)  # 1 token / 2s
        assert await rl.try_consume("x") is True
        return await rl.retry_after("x")

    ra = asyncio.run(_go())
    assert 1.9 <= ra <= 2.1, ra


def test_rate_limiter_async_lock_is_asyncio_lock():
    """Structural invariant: the async-path lock must be ``asyncio.Lock``.

    Catches the most common regression — someone "fixing" a typo by
    swapping the lock back to ``threading.Lock`` because "the tests
    pass either way". They pass because the critical section is so
    fast the difference is unobservable; they would NOT pass if the
    server were under real concurrent load and the kernel mutex
    parked the event loop on contended sockets.
    """
    rl = RateLimiter(capacity=10, refill_per_second=1.0)
    assert isinstance(rl._async_lock, asyncio.Lock), (
        f"async-path lock must be asyncio.Lock, got {type(rl._async_lock)}"
    )


def test_rate_limiter_async_100_concurrent_consumes_complete_correctly():
    """100 concurrent ``try_consume`` calls on DISTINCT keys all succeed.

    What this test catches:
      * Deadlock: 100 coroutines blocked on a misconfigured async lock
        would never resolve — ``asyncio.run`` times out (we add an
        explicit ``wait_for`` so a deadlock manifests as a failed test
        rather than a hanging CI run).
      * Critical-section corruption: every distinct-key consume should
        return True (capacity is per-key, so a per-key bucket starts
        full). Anything < 100 truthy results indicates the consume
        logic is racing.

    The wall-time bound is intentionally absolute (1 second), not
    relative to single-call cost — single-call cost on a modern CPU is
    ~2us, which is comparable to ``perf_counter`` resolution and makes
    relative comparisons noisy. 100 dictionary ops + 100 async
    coroutine schedules should finish in milliseconds, not seconds.
    A breach of the 1-second budget means the event loop was actually
    parked, not jittery clock measurement.
    """
    rl = RateLimiter(capacity=10, refill_per_second=1.0)

    async def _flood():
        tasks = [rl.try_consume(f"signer_{i}") for i in range(100)]
        return await asyncio.wait_for(asyncio.gather(*tasks), timeout=5.0)

    t0 = time.perf_counter()
    results = asyncio.run(_flood())
    elapsed = time.perf_counter() - t0

    # Correctness: distinct keys, fresh buckets — all 100 must succeed.
    assert all(results), (
        f"expected 100 successes, got {sum(1 for r in results if r)}: "
        "consume logic is racing"
    )
    assert len(results) == 100

    # Absolute wall-time bound. 100 dict ops + 100 coroutine schedules
    # should not take 1 second. If it does, the loop was parked.
    assert elapsed < 1.0, (
        f"100 async consumes took {elapsed:.4f}s — far over the 1s budget. "
        f"The event loop was likely parked on a sync lock."
    )


def test_rate_limiter_async_does_not_interleave_critical_section():
    """Repeated concurrent consumes on the SAME key must not exceed capacity.

    asyncio.Lock guarantees mutual exclusion across coroutines on the same
    event loop. If we accidentally removed the lock entirely, two
    coroutines could each read ``b.tokens == 1`` between the read and the
    decrement, both observing "tokens available" before the first decrement
    landed. Capacity=1 + 50 concurrent consumes → exactly one must succeed.
    """

    async def _go():
        rl = RateLimiter(capacity=1, refill_per_second=0.001)
        tasks = [rl.try_consume("hot_key") for _ in range(50)]
        return await asyncio.gather(*tasks)

    results = asyncio.run(_go())
    # Exactly one True in the result set; the rest are False.
    assert sum(1 for r in results if r) == 1, results
