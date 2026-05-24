"""Per-key token-bucket rate limiter.

Phase-2 Slice-5B hardening. Used by ``DarkPoolServer`` to throttle the
``/query`` endpoint per signer address, so a single misbehaving (or
compromised) EOA can't drain Alice's service capacity even if it holds
infinite signed authorisations.

Algorithm — classic token bucket:

    tokens_now = min(capacity, tokens_last + (now - t_last) * refill_per_second)

When ``try_consume(tokens)`` is called we lazily refill the bucket, then
deduct ``tokens`` if available. No background timer; refills are
computed on demand.

Phase 5 Stream D — Concurrency model:

The limiter is invoked from inside ``async def`` request handlers
(``DarkPoolServer._handle_query`` / ``_handle_add``). The previous
``threading.Lock`` blocked the event loop on every consume, which under
contention serialised every request behind a kernel-level mutex even
though asyncio is single-threaded. We now expose **async** versions of
``try_consume`` and ``retry_after`` guarded by an ``asyncio.Lock`` so
multiple concurrent requests interleave at the await points instead of
parking the loop.

Synchronous wrappers (``try_consume_sync`` / ``retry_after_sync``) are
kept for callers that are NOT in an event loop (unit tests using
deterministic monkeypatched clocks). They use a plain ``threading.Lock``
as a backstop, because mixing a ``threading.Lock`` with an
``asyncio.Lock`` over the same data is incorrect — the asyncio lock is
not aware of OS threads, and vice versa.

Both APIs share the same bucket dict; consistency between sync and
async paths is maintained by holding only ONE lock at a time. Tests
exercise sync paths only; the production handler exercises async paths
only. We do NOT mix them on the same RateLimiter instance.
"""

from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass


@dataclass
class _Bucket:
    tokens: float
    last_refill: float


class RateLimiter:
    """Token-bucket limiter keyed by an arbitrary string.

    Args:
        capacity: maximum tokens the bucket can hold. A fresh key starts
            full (``tokens == capacity``).
        refill_per_second: how many tokens accrue per real second.
            Fractional values are fine — a value of ``1.0`` means one
            token every second.
    """

    def __init__(self, capacity: int, refill_per_second: float) -> None:
        if capacity <= 0:
            raise ValueError("capacity must be > 0")
        if refill_per_second <= 0:
            raise ValueError("refill_per_second must be > 0")
        self.capacity = int(capacity)
        self.refill_per_second = float(refill_per_second)
        self._buckets: dict[str, _Bucket] = {}
        # Sync lock for the sync entry points (used by unit tests and the
        # deterministic monkeypatched-clock test suite). Held only for the
        # synchronous critical section in *_sync methods.
        self._sync_lock = threading.Lock()
        # Async lock for the async entry points (held inside ``async def``
        # handlers on the FastAPI request path). asyncio.Lock instances are
        # lazily bound to the running event loop on first acquire — we
        # construct it eagerly because asyncio.Lock() in modern Python
        # (3.10+) no longer requires a running loop at construction time.
        self._async_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Time hook — overridden in tests via monkeypatch for determinism.
    # ------------------------------------------------------------------
    def _now(self) -> float:
        return time.monotonic()

    # ------------------------------------------------------------------
    # Internal bucket update — pure, no locking. Caller must hold the
    # appropriate lock.
    # ------------------------------------------------------------------

    def _consume_locked(self, key: str, tokens: int, now: float) -> bool:
        b = self._buckets.get(key)
        if b is None:
            b = _Bucket(tokens=float(self.capacity), last_refill=now)
            self._buckets[key] = b
        else:
            elapsed = max(0.0, now - b.last_refill)
            b.tokens = min(
                float(self.capacity),
                b.tokens + elapsed * self.refill_per_second,
            )
            b.last_refill = now
        if b.tokens >= tokens:
            b.tokens -= tokens
            return True
        return False

    def _retry_after_locked(self, key: str, tokens: int, now: float) -> float:
        b = self._buckets.get(key)
        if b is None:
            # A brand-new bucket would start full.
            return 0.0 if tokens <= self.capacity else float("inf")
        elapsed = max(0.0, now - b.last_refill)
        projected = min(
            float(self.capacity), b.tokens + elapsed * self.refill_per_second
        )
        if projected >= tokens:
            return 0.0
        deficit = tokens - projected
        return deficit / self.refill_per_second

    # ------------------------------------------------------------------
    # Async API — used by DarkPoolServer._handle_query / _handle_add.
    # ------------------------------------------------------------------

    async def try_consume(self, key: str, tokens: int = 1) -> bool:
        """Async: attempt to take ``tokens`` from ``key``'s bucket.

        Returns ``True`` on success, ``False`` otherwise. The critical
        section uses an ``asyncio.Lock`` rather than a ``threading.Lock``
        so concurrent ``await``s on this method interleave at the lock
        instead of blocking the event loop on a kernel mutex.
        """
        if tokens <= 0:
            raise ValueError("tokens must be > 0")
        now = self._now()
        async with self._async_lock:
            return self._consume_locked(key, tokens, now)

    async def retry_after(self, key: str, tokens: int = 1) -> float:
        """Async: seconds until ``key`` can next consume ``tokens``."""
        if tokens <= 0:
            raise ValueError("tokens must be > 0")
        now = self._now()
        async with self._async_lock:
            return self._retry_after_locked(key, tokens, now)

    # ------------------------------------------------------------------
    # Sync API — used by unit tests with a deterministic monkeypatched
    # clock. NOT used from inside ``async def`` handlers.
    # ------------------------------------------------------------------

    def try_consume_sync(self, key: str, tokens: int = 1) -> bool:
        """Sync variant of :meth:`try_consume`.

        Used by deterministic unit tests that drive the limiter from
        synchronous test bodies. Do NOT call from an ``async def``
        handler — that's what :meth:`try_consume` is for.
        """
        if tokens <= 0:
            raise ValueError("tokens must be > 0")
        now = self._now()
        with self._sync_lock:
            return self._consume_locked(key, tokens, now)

    def retry_after_sync(self, key: str, tokens: int = 1) -> float:
        """Sync variant of :meth:`retry_after`."""
        if tokens <= 0:
            raise ValueError("tokens must be > 0")
        now = self._now()
        with self._sync_lock:
            return self._retry_after_locked(key, tokens, now)

    def reset(self, key: str | None = None) -> None:
        """Reset one key (or all keys when ``key is None``).

        Uses the sync lock — safe to call from either sync or async
        code, but must not be awaited (it does not yield).
        """
        with self._sync_lock:
            if key is None:
                self._buckets.clear()
            else:
                self._buckets.pop(key, None)

    # ------------------------------------------------------------------
    # Introspection helpers (tests + observability)
    # ------------------------------------------------------------------

    def tokens(self, key: str) -> float:
        """Lazily-refilled current token balance for ``key`` (read-only)."""
        now = self._now()
        with self._sync_lock:
            b = self._buckets.get(key)
            if b is None:
                return float(self.capacity)
            elapsed = max(0.0, now - b.last_refill)
            return min(
                float(self.capacity),
                b.tokens + elapsed * self.refill_per_second,
            )

    def __len__(self) -> int:
        with self._sync_lock:
            return len(self._buckets)
