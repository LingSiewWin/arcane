"""Persistent nonce replay-protection store.

Phase-2 Slice-5B hardening — replaces the in-memory ``set`` previously used
by ``DarkPoolServer`` with a real SQLite-backed store so that nonces are
preserved across server restarts.

Phase-3 F2 hardening — nonce identity is now namespaced by
``(chain_id, verifying_contract, signer, nonce)`` so that the same nonce
issued under one EIP-712 domain cannot be replayed under a different
domain (e.g. a different chain or a different token contract).

Two implementations are provided:

* ``SqliteNonceStore(path)`` — real sqlite3 file, WAL mode for concurrent
  reads, a single connection guarded by a lock for serialised writes.
* ``InMemoryNonceStore()`` — lightweight in-process variant, useful for
  ephemeral tests and the hot path of unit tests that don't care about
  persistence.

Both implement the same ``NonceStore`` Protocol so the dark pool can swap
between them at construction time.

Backward compatibility: callers that pass only ``(signer, nonce)`` are
mapped into a sentinel legacy domain ``(0, "0x0...0")`` and a
``DeprecationWarning`` is emitted. The legacy domain is its own
namespace — rows written there can never collide with a real chain's
rows.
"""

from __future__ import annotations

import os
import sqlite3
import sys
import threading
import warnings
from typing import Protocol, runtime_checkable


_SCHEMA_VERSION = 2

# Sentinel domain used when callers don't provide chain_id /
# verifying_contract. The (chain_id=0, verifying_contract=0x0..0) pair is
# disjoint from any real EIP-712 domain because legitimate domains have
# non-zero chain IDs and non-zero contract addresses.
_LEGACY_CHAIN_ID = 0
_LEGACY_VERIFYING_CONTRACT = "0x" + "0" * 40


@runtime_checkable
class NonceStore(Protocol):
    """A minimal persistence interface for replay-protection nonces."""

    def has(
        self,
        signer: str,
        nonce: str,
        chain_id: int | None = None,
        verifying_contract: str | None = None,
    ) -> bool: ...

    def add(
        self,
        signer: str,
        nonce: str,
        expires_at: int,
        chain_id: int | None = None,
        verifying_contract: str | None = None,
    ) -> None: ...

    def purge_expired(self, now: int) -> int:
        """Remove rows whose ``expires_at <= now``. Returns rows purged."""
        ...

    def close(self) -> None: ...


_SCHEMA_DDL = (
    """
    CREATE TABLE IF NOT EXISTS nonces (
        chain_id           INTEGER NOT NULL,
        verifying_contract TEXT    NOT NULL,
        signer             TEXT    NOT NULL,
        nonce              TEXT    NOT NULL,
        expires_at         INTEGER NOT NULL,
        PRIMARY KEY (chain_id, verifying_contract, signer, nonce)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_nonce_expiry ON nonces(expires_at)",
    """
    CREATE TABLE IF NOT EXISTS nonce_store_meta (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
)


def _normalise(signer: str, nonce: str) -> tuple[str, str]:
    """Always lowercase hex addresses + nonces so casing doesn't matter."""
    return signer.lower(), nonce.lower()


def _warn_legacy() -> None:
    """Emit a DeprecationWarning for callers using the 2-arg form."""
    warnings.warn(
        "NonceStore.has/.add called without (chain_id, verifying_contract); "
        "rows will be stored under the legacy sentinel domain. Update callers "
        "to pass the EIP-712 domain to prevent cross-domain replay.",
        DeprecationWarning,
        stacklevel=3,
    )


def _resolve_domain(
    chain_id: int | None,
    verifying_contract: str | None,
) -> tuple[int, str, bool]:
    """Resolve (chain_id, verifying_contract) to a stored domain key.

    Returns ``(resolved_chain_id, resolved_verifying_contract_lc, used_legacy)``.
    """
    used_legacy = False
    if chain_id is None or verifying_contract is None:
        used_legacy = True
        return _LEGACY_CHAIN_ID, _LEGACY_VERIFYING_CONTRACT, used_legacy
    return int(chain_id), str(verifying_contract).lower(), used_legacy


class SqliteNonceStore:
    """SQLite-backed nonce store.

    The store opens a single connection (``check_same_thread=False``) and
    serialises all access through ``self._lock``. SQLite's WAL mode is
    enabled so the file remains readable concurrently even while we hold
    the write lock on the Python side.

    The file path lives on disk: pass ``":memory:"`` only in tests where
    you don't care about persistence across reopens (the sqlite ``:memory:``
    database is per-connection and is wiped when this object is closed).
    """

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self._path = os.fspath(path)
        self._lock = threading.Lock()

        # Migration step: if a v1-schema file exists at this path, drop it
        # (and its sidecars) so we don't try to read an incompatible
        # layout. ``:memory:`` is exempt — there's nothing on disk.
        if self._path != ":memory:":
            self._maybe_migrate_legacy_file()

        # ``check_same_thread=False`` because FastAPI / uvicorn may invoke
        # us from threadpool workers; we rely on the lock for safety.
        self._conn = sqlite3.connect(
            self._path,
            check_same_thread=False,
            isolation_level=None,  # autocommit; we manage transactions
        )
        # WAL gives us concurrent reads while a writer is active.
        # ``:memory:`` databases reject WAL — fall back to default journaling
        # for that case (tests only).
        if self._path != ":memory:":
            try:
                self._conn.execute("PRAGMA journal_mode=WAL")
            except sqlite3.DatabaseError:
                pass
        self._conn.execute("PRAGMA synchronous=NORMAL")
        for ddl in _SCHEMA_DDL:
            self._conn.execute(ddl)

        # Stamp the schema version (idempotent).
        self._conn.execute(
            "INSERT OR REPLACE INTO nonce_store_meta(key, value) VALUES (?, ?)",
            ("schema_version", str(_SCHEMA_VERSION)),
        )

    # ------------------------------------------------------------------
    # Migration
    # ------------------------------------------------------------------

    def _maybe_migrate_legacy_file(self) -> None:
        """If the on-disk file exists but predates v2, drop it.

        We detect "predates v2" as either: the meta table doesn't exist,
        or it does exist and ``schema_version`` is missing or < 2.
        """
        if not os.path.exists(self._path):
            return

        try:
            probe = sqlite3.connect(self._path)
        except sqlite3.DatabaseError:
            # Corrupt file — treat as legacy and replace.
            self._drop_legacy_file("corrupt sqlite file")
            return

        try:
            cur = probe.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='nonce_store_meta'"
            )
            row = cur.fetchone()
            if row is None:
                # No meta table => pre-v2.
                probe.close()
                self._drop_legacy_file("no nonce_store_meta table")
                return

            cur = probe.execute(
                "SELECT value FROM nonce_store_meta WHERE key='schema_version'"
            )
            ver_row = cur.fetchone()
            try:
                version = int(ver_row[0]) if ver_row else 0
            except (TypeError, ValueError):
                version = 0
        finally:
            try:
                probe.close()
            except sqlite3.Error:
                pass

        if version < _SCHEMA_VERSION:
            self._drop_legacy_file(f"schema_version={version} < {_SCHEMA_VERSION}")

    def _drop_legacy_file(self, why: str) -> None:
        """Unlink the on-disk DB plus its sidecars, with a stderr warning."""
        print(
            f"[nonce_store] dropping legacy DB at {self._path} ({why}); "
            f"nonces from previous schema will not be carried over.",
            file=sys.stderr,
        )
        for suffix in ("", "-wal", "-shm", "-journal"):
            sidecar = self._path + suffix
            try:
                os.unlink(sidecar)
            except FileNotFoundError:
                pass
            except OSError:
                # Best-effort: if we can't drop a sidecar we'd rather
                # surface the sqlite error later than crash here.
                pass

    # ------------------------------------------------------------------
    # NonceStore interface
    # ------------------------------------------------------------------

    def has(
        self,
        signer: str,
        nonce: str,
        chain_id: int | None = None,
        verifying_contract: str | None = None,
    ) -> bool:
        cid, vc, used_legacy = _resolve_domain(chain_id, verifying_contract)
        if used_legacy:
            _warn_legacy()
        s, n = _normalise(signer, nonce)
        with self._lock:
            cur = self._conn.execute(
                "SELECT 1 FROM nonces "
                "WHERE chain_id = ? AND verifying_contract = ? "
                "  AND signer = ? AND nonce = ? LIMIT 1",
                (cid, vc, s, n),
            )
            return cur.fetchone() is not None

    def add(
        self,
        signer: str,
        nonce: str,
        expires_at: int,
        chain_id: int | None = None,
        verifying_contract: str | None = None,
    ) -> None:
        cid, vc, used_legacy = _resolve_domain(chain_id, verifying_contract)
        if used_legacy:
            _warn_legacy()
        s, n = _normalise(signer, nonce)
        with self._lock:
            # INSERT OR IGNORE so concurrent inserts of the same nonce
            # don't raise — has() is still the source of truth for the
            # "already seen" question.
            self._conn.execute(
                "INSERT OR IGNORE INTO nonces "
                "(chain_id, verifying_contract, signer, nonce, expires_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (cid, vc, s, n, int(expires_at)),
            )

    def purge_expired(self, now: int) -> int:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM nonces WHERE expires_at <= ?", (int(now),)
            )
            return int(cur.rowcount or 0)

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass

    # ------------------------------------------------------------------
    # Test / introspection helpers
    # ------------------------------------------------------------------

    def __len__(self) -> int:
        with self._lock:
            cur = self._conn.execute("SELECT COUNT(*) FROM nonces")
            row = cur.fetchone()
            return int(row[0]) if row else 0

    @property
    def path(self) -> str:
        return self._path


class InMemoryNonceStore:
    """Process-local nonce store. Resets on process exit.

    Useful for tests that don't care about persistence; production code
    paths should prefer ``SqliteNonceStore``.
    """

    def __init__(self) -> None:
        # (chain_id, verifying_contract, signer, nonce) -> expires_at
        self._rows: dict[tuple[int, str, str, str], int] = {}
        self._lock = threading.Lock()

    def has(
        self,
        signer: str,
        nonce: str,
        chain_id: int | None = None,
        verifying_contract: str | None = None,
    ) -> bool:
        cid, vc, used_legacy = _resolve_domain(chain_id, verifying_contract)
        if used_legacy:
            _warn_legacy()
        s, n = _normalise(signer, nonce)
        key = (cid, vc, s, n)
        with self._lock:
            return key in self._rows

    def add(
        self,
        signer: str,
        nonce: str,
        expires_at: int,
        chain_id: int | None = None,
        verifying_contract: str | None = None,
    ) -> None:
        cid, vc, used_legacy = _resolve_domain(chain_id, verifying_contract)
        if used_legacy:
            _warn_legacy()
        s, n = _normalise(signer, nonce)
        key = (cid, vc, s, n)
        with self._lock:
            # First write wins, matching SQLite's INSERT OR IGNORE.
            self._rows.setdefault(key, int(expires_at))

    def purge_expired(self, now: int) -> int:
        with self._lock:
            stale = [k for k, exp in self._rows.items() if exp <= now]
            for k in stale:
                del self._rows[k]
            return len(stale)

    def close(self) -> None:
        with self._lock:
            self._rows.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._rows)
