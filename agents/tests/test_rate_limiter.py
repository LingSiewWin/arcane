"""Tests for ``agents.rate_limiter``.

The token-bucket uses ``time.monotonic()`` internally; for deterministic
refill behaviour the tests monkeypatch the ``_now`` hook so we don't have
to actually sleep.
"""

from __future__ import annotations

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
        assert rl.try_consume("alice") is True
    # 4th in the same instant must fail — bucket empty.
    assert rl.try_consume("alice") is False


def test_rate_limiter_refills_over_time(fake_clock):
    rl = RateLimiter(capacity=2, refill_per_second=1.0)
    assert rl.try_consume("bob")
    assert rl.try_consume("bob")
    assert rl.try_consume("bob") is False

    # Advance 1 second — should accrue exactly one token.
    fake_clock["t"] += 1.0
    assert rl.try_consume("bob") is True
    # And we're empty again immediately after.
    assert rl.try_consume("bob") is False


def test_rate_limiter_per_key_isolation(fake_clock):
    rl = RateLimiter(capacity=2, refill_per_second=1.0)
    assert rl.try_consume("alice")
    assert rl.try_consume("alice")
    assert rl.try_consume("alice") is False
    # Bob is unaffected.
    assert rl.try_consume("bob") is True
    assert rl.try_consume("bob") is True
    assert rl.try_consume("bob") is False


def test_rate_limiter_capacity_caps_refill(fake_clock):
    """Even with a huge time jump the bucket never goes above capacity."""
    rl = RateLimiter(capacity=5, refill_per_second=100.0)
    # Drain.
    for _ in range(5):
        assert rl.try_consume("c")
    # Advance an hour — bucket should not exceed 5 tokens.
    fake_clock["t"] += 3600
    # 5 must succeed, the 6th must fail.
    for _ in range(5):
        assert rl.try_consume("c") is True
    assert rl.try_consume("c") is False


def test_rate_limiter_reset_single_key(fake_clock):
    rl = RateLimiter(capacity=1, refill_per_second=0.001)
    assert rl.try_consume("alice")
    assert rl.try_consume("alice") is False
    # Reset specifically Alice.
    rl.reset("alice")
    assert rl.try_consume("alice") is True


def test_rate_limiter_reset_all(fake_clock):
    rl = RateLimiter(capacity=1, refill_per_second=0.001)
    assert rl.try_consume("alice")
    assert rl.try_consume("bob")
    assert rl.try_consume("alice") is False
    assert rl.try_consume("bob") is False
    rl.reset()  # reset all
    assert rl.try_consume("alice")
    assert rl.try_consume("bob")


def test_rate_limiter_consume_multiple_tokens(fake_clock):
    rl = RateLimiter(capacity=10, refill_per_second=1.0)
    assert rl.try_consume("x", tokens=4)
    assert rl.try_consume("x", tokens=4)
    # 8 consumed; only 2 left; asking for 3 must fail.
    assert rl.try_consume("x", tokens=3) is False
    # Confirm bucket unchanged on failed deduction.
    assert rl.try_consume("x", tokens=2) is True


def test_rate_limiter_retry_after_reports_real_seconds(fake_clock):
    rl = RateLimiter(capacity=1, refill_per_second=0.5)  # 1 token / 2s
    assert rl.try_consume("x")
    # Bucket empty — retry-after should be ~2 seconds.
    ra = rl.retry_after("x")
    assert 1.9 <= ra <= 2.1, ra


def test_rate_limiter_rejects_bad_args():
    with pytest.raises(ValueError):
        RateLimiter(capacity=0, refill_per_second=1.0)
    with pytest.raises(ValueError):
        RateLimiter(capacity=1, refill_per_second=0)
    rl = RateLimiter(capacity=1, refill_per_second=1.0)
    with pytest.raises(ValueError):
        rl.try_consume("x", tokens=0)


def test_rate_limiter_real_clock_smoke():
    """Sanity check with the real monotonic clock — no fake."""
    rl = RateLimiter(capacity=2, refill_per_second=50.0)
    assert rl.try_consume("k")
    assert rl.try_consume("k")
    assert rl.try_consume("k") is False
    # Sleep enough for one refill (1/50 s = 20ms — give 30ms).
    time.sleep(0.03)
    assert rl.try_consume("k") is True
