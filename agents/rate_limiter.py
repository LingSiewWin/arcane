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
"""

from __future__ import annotations

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
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Time hook — overridden in tests via monkeypatch for determinism.
    # ------------------------------------------------------------------
    def _now(self) -> float:
        return time.monotonic()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def try_consume(self, key: str, tokens: int = 1) -> bool:
        """Attempt to take ``tokens`` from ``key``'s bucket.

        Returns ``True`` on success (bucket was non-empty enough and the
        deduction has been applied), ``False`` otherwise. On ``False``
        the bucket is left unchanged.
        """
        if tokens <= 0:
            raise ValueError("tokens must be > 0")
        now = self._now()
        with self._lock:
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

    def retry_after(self, key: str, tokens: int = 1) -> float:
        """Seconds until ``key`` can next consume ``tokens``.

        If the bucket already has enough tokens this returns ``0.0``.
        Otherwise the value is the minimum real time the caller needs
        to wait before another ``try_consume`` could succeed.
        """
        if tokens <= 0:
            raise ValueError("tokens must be > 0")
        now = self._now()
        with self._lock:
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

    def reset(self, key: str | None = None) -> None:
        """Reset one key (or all keys when ``key is None``)."""
        with self._lock:
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
        with self._lock:
            b = self._buckets.get(key)
            if b is None:
                return float(self.capacity)
            elapsed = max(0.0, now - b.last_refill)
            return min(
                float(self.capacity),
                b.tokens + elapsed * self.refill_per_second,
            )

    def __len__(self) -> int:
        with self._lock:
            return len(self._buckets)
