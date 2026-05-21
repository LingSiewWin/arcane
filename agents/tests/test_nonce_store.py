"""Tests for ``agents.nonce_store``.

Real sqlite3 file I/O — no mocks. The persistence test physically closes
the DB, drops the Python reference, and reopens the same file.
"""

from __future__ import annotations

import os
import sqlite3
import time
import warnings

import pytest

from agents.nonce_store import (
    InMemoryNonceStore,
    NonceStore,
    SqliteNonceStore,
    _LEGACY_CHAIN_ID,
    _LEGACY_VERIFYING_CONTRACT,
    _SCHEMA_VERSION,
)


SIGNER_A = "0xabc0000000000000000000000000000000000001"
SIGNER_B = "0xabc0000000000000000000000000000000000002"

TOKEN_A = "0x1111111111111111111111111111111111111111"
TOKEN_B = "0x2222222222222222222222222222222222222222"


def _nonce(suffix: str) -> str:
    # Pad to 32 bytes hex for realism with EIP-3009 nonces.
    raw = suffix.encode().hex()
    return "0x" + raw.ljust(64, "0")


@pytest.fixture(autouse=True)
def _silence_legacy_warning():
    """The existing 2-arg-form tests exercise the backward-compat path on
    purpose; suppress the DeprecationWarning noise globally and let the
    explicit new tests opt back in via ``warnings.catch_warnings``."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        yield


# ---------------------------------------------------------------------------
# SqliteNonceStore — legacy 2-arg form (backward compat)
# ---------------------------------------------------------------------------


def test_sqlite_basic_add_and_has(tmp_path):
    db = tmp_path / "nonces.db"
    store = SqliteNonceStore(str(db))
    try:
        assert store.has(SIGNER_A, _nonce("n1")) is False
        store.add(SIGNER_A, _nonce("n1"), expires_at=int(time.time()) + 600)
        assert store.has(SIGNER_A, _nonce("n1")) is True
        # Different signer, same nonce — different row.
        assert store.has(SIGNER_B, _nonce("n1")) is False
    finally:
        store.close()


def test_sqlite_persists_across_close_and_reopen(tmp_path):
    """The whole point of this slice: nonce survives a restart."""
    db = tmp_path / "nonces.db"
    nonce = _nonce("persist")
    expires = int(time.time()) + 3600

    # 1. Write nonce, close connection.
    store_v1 = SqliteNonceStore(str(db))
    store_v1.add(SIGNER_A, nonce, expires_at=expires)
    assert store_v1.has(SIGNER_A, nonce)
    store_v1.close()
    del store_v1

    # 2. File must still exist on disk.
    assert db.exists()
    assert os.path.getsize(str(db)) > 0

    # 3. Reopen the SAME file with a brand-new connection — replay
    # protection must remember the nonce.
    store_v2 = SqliteNonceStore(str(db))
    try:
        assert store_v2.has(SIGNER_A, nonce) is True
        # Wrong signer must still be a miss.
        assert store_v2.has(SIGNER_B, nonce) is False
    finally:
        store_v2.close()


def test_sqlite_purge_expired_removes_old_rows(tmp_path):
    db = tmp_path / "nonces.db"
    store = SqliteNonceStore(str(db))
    try:
        now = int(time.time())
        store.add(SIGNER_A, _nonce("old1"), expires_at=now - 100)
        store.add(SIGNER_A, _nonce("old2"), expires_at=now - 1)
        store.add(SIGNER_A, _nonce("fresh"), expires_at=now + 1000)
        assert len(store) == 3

        purged = store.purge_expired(now)
        assert purged == 2
        assert len(store) == 1
        assert store.has(SIGNER_A, _nonce("fresh"))
        assert not store.has(SIGNER_A, _nonce("old1"))
        assert not store.has(SIGNER_A, _nonce("old2"))

        # Idempotent — second purge removes nothing.
        assert store.purge_expired(now) == 0
    finally:
        store.close()


def test_sqlite_duplicate_add_is_idempotent(tmp_path):
    db = tmp_path / "nonces.db"
    store = SqliteNonceStore(str(db))
    try:
        n = _nonce("dup")
        store.add(SIGNER_A, n, expires_at=1000)
        # Second add must not throw; ``has`` still True.
        store.add(SIGNER_A, n, expires_at=2000)
        assert store.has(SIGNER_A, n)
        assert len(store) == 1
    finally:
        store.close()


def test_sqlite_case_insensitive_lookup(tmp_path):
    db = tmp_path / "nonces.db"
    store = SqliteNonceStore(str(db))
    try:
        store.add(
            "0xABCDEF0000000000000000000000000000000001",
            "0xDEADBEEF",
            expires_at=9999999999,
        )
        # Same value, different casing must collide (matches ecrecover
        # output which can be checksummed or lowercase).
        assert store.has(
            "0xabcdef0000000000000000000000000000000001", "0xdeadbeef"
        )
        assert store.has(
            "0xAbCdEf0000000000000000000000000000000001", "0xDeAdBeEf"
        )
    finally:
        store.close()


def test_sqlite_close_is_idempotent(tmp_path):
    db = tmp_path / "nonces.db"
    store = SqliteNonceStore(str(db))
    store.close()
    # A second close must not raise.
    store.close()


# ---------------------------------------------------------------------------
# InMemoryNonceStore — sanity for the Protocol-conformant variant
# ---------------------------------------------------------------------------


def test_inmemory_store_basic():
    store = InMemoryNonceStore()
    try:
        # Conforms to the Protocol.
        assert isinstance(store, NonceStore)

        n = _nonce("mem")
        assert not store.has(SIGNER_A, n)
        store.add(SIGNER_A, n, expires_at=int(time.time()) + 60)
        assert store.has(SIGNER_A, n)
        assert not store.has(SIGNER_B, n)

        # Purge.
        store.add(SIGNER_B, _nonce("expired"), expires_at=1)
        purged = store.purge_expired(int(time.time()))
        assert purged == 1
        assert not store.has(SIGNER_B, _nonce("expired"))
    finally:
        store.close()


def test_inmemory_close_clears_state():
    store = InMemoryNonceStore()
    store.add(SIGNER_A, _nonce("x"), expires_at=9999999999)
    assert len(store) == 1
    store.close()
    assert len(store) == 0


def test_inmemory_and_sqlite_share_protocol(tmp_path):
    """Both implementations must satisfy ``NonceStore``."""
    sqlite_store = SqliteNonceStore(str(tmp_path / "x.db"))
    try:
        assert isinstance(sqlite_store, NonceStore)
        assert isinstance(InMemoryNonceStore(), NonceStore)
    finally:
        sqlite_store.close()


def test_sqlite_wal_mode_enabled(tmp_path):
    """WAL mode is required for concurrent reads while a writer is active."""
    db = tmp_path / "nonces.db"
    store = SqliteNonceStore(str(db))
    try:
        cur = store._conn.execute("PRAGMA journal_mode")
        mode = cur.fetchone()[0]
        # On macOS / Linux file paths WAL must succeed.
        assert mode.lower() == "wal", f"expected wal journal_mode, got {mode}"
    finally:
        store.close()


@pytest.mark.parametrize("klass", [SqliteNonceStore, InMemoryNonceStore])
def test_store_has_returns_false_for_unknown(tmp_path, klass):
    store = klass(str(tmp_path / "u.db")) if klass is SqliteNonceStore else klass()
    try:
        assert store.has(SIGNER_A, _nonce("never_added")) is False
    finally:
        store.close()


# ---------------------------------------------------------------------------
# F2 — cross-domain replay protection
# ---------------------------------------------------------------------------


def test_chain_id_namespacing_prevents_replay(tmp_path):
    """A nonce written under chain_id=1 must NOT count as seen on chain_id=2."""
    db = tmp_path / "x.db"
    store = SqliteNonceStore(str(db))
    try:
        n = _nonce("xchain")
        store.add(
            SIGNER_A, n, expires_at=9999999999,
            chain_id=1, verifying_contract=TOKEN_A,
        )
        assert store.has(SIGNER_A, n, chain_id=1, verifying_contract=TOKEN_A)
        # Different chain_id, same token + signer + nonce: NEW domain.
        assert not store.has(
            SIGNER_A, n, chain_id=2, verifying_contract=TOKEN_A
        )
    finally:
        store.close()


def test_verifying_contract_namespacing_prevents_replay(tmp_path):
    """A nonce written under TOKEN_A must NOT count as seen on TOKEN_B."""
    db = tmp_path / "x.db"
    store = SqliteNonceStore(str(db))
    try:
        n = _nonce("xtoken")
        store.add(
            SIGNER_A, n, expires_at=9999999999,
            chain_id=84532, verifying_contract=TOKEN_A,
        )
        assert store.has(
            SIGNER_A, n, chain_id=84532, verifying_contract=TOKEN_A
        )
        assert not store.has(
            SIGNER_A, n, chain_id=84532, verifying_contract=TOKEN_B
        )
    finally:
        store.close()


def test_inmemory_chain_and_token_namespacing():
    """Same namespacing semantics for the in-memory store."""
    store = InMemoryNonceStore()
    try:
        n = _nonce("mem_xdom")
        store.add(
            SIGNER_A, n, expires_at=9999999999,
            chain_id=1, verifying_contract=TOKEN_A,
        )
        # Same domain hits.
        assert store.has(
            SIGNER_A, n, chain_id=1, verifying_contract=TOKEN_A
        )
        # Other chain: miss.
        assert not store.has(
            SIGNER_A, n, chain_id=8453, verifying_contract=TOKEN_A
        )
        # Other token: miss.
        assert not store.has(
            SIGNER_A, n, chain_id=1, verifying_contract=TOKEN_B
        )
    finally:
        store.close()


def test_legacy_two_arg_form_still_works(tmp_path):
    """The 2-arg ``has(signer, nonce)`` / ``add(signer, nonce, exp)`` calls
    used by sibling-owned ``dark_pool.py`` must keep working — but they
    must also emit a ``DeprecationWarning`` so callers know to migrate."""
    db = tmp_path / "legacy.db"
    store = SqliteNonceStore(str(db))
    try:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", DeprecationWarning)
            store.add(SIGNER_A, _nonce("legacy"), expires_at=9999999999)
            assert store.has(SIGNER_A, _nonce("legacy")) is True
            assert store.has(SIGNER_A, _nonce("never")) is False
        # At least one DeprecationWarning got recorded.
        dep_warnings = [w for w in caught if issubclass(w.category, DeprecationWarning)]
        assert dep_warnings, "expected DeprecationWarning for 2-arg form"
    finally:
        store.close()


def test_legacy_and_namespaced_are_distinct_rows(tmp_path):
    """A row written via the legacy 2-arg form lives in the sentinel domain
    and MUST NOT collide with a row written under a real domain."""
    db = tmp_path / "split.db"
    store = SqliteNonceStore(str(db))
    try:
        n = _nonce("split")
        # Legacy write -> sentinel domain.
        store.add(SIGNER_A, n, expires_at=9999999999)
        # Real-domain has() must return False — the sentinel row doesn't
        # leak into namespaced lookups.
        assert not store.has(
            SIGNER_A, n, chain_id=1, verifying_contract=TOKEN_A
        )
        # And the legacy 2-arg has() still finds the sentinel row.
        assert store.has(SIGNER_A, n)
        # We can also probe the sentinel domain explicitly.
        assert store.has(
            SIGNER_A, n,
            chain_id=_LEGACY_CHAIN_ID,
            verifying_contract=_LEGACY_VERIFYING_CONTRACT,
        )
    finally:
        store.close()


def test_schema_migration_drops_legacy_v1_file(tmp_path, capsys):
    """If we encounter a v1-format DB on disk (old PK, no meta table), the
    store must drop the file, warn to stderr, and create a fresh v2 schema
    that accepts namespaced writes."""
    db = tmp_path / "legacy_v1.db"

    # Hand-craft a v1 schema (no meta table, old PK).
    conn = sqlite3.connect(str(db))
    conn.execute(
        """
        CREATE TABLE nonces (
            signer TEXT NOT NULL,
            nonce TEXT NOT NULL,
            expires_at INTEGER NOT NULL,
            PRIMARY KEY (signer, nonce)
        )
        """
    )
    conn.execute(
        "INSERT INTO nonces(signer, nonce, expires_at) VALUES (?, ?, ?)",
        (SIGNER_A, _nonce("v1row"), 9999999999),
    )
    conn.commit()
    conn.close()
    assert db.exists()

    # Now open with the v2 store — it must wipe and recreate.
    store = SqliteNonceStore(str(db))
    try:
        # Old v1 rows must be gone.
        assert len(store) == 0
        # Capture stderr migration warning.
        captured = capsys.readouterr()
        assert "dropping legacy DB" in captured.err

        # New namespaced writes work.
        store.add(
            SIGNER_A,
            _nonce("v2row"),
            expires_at=9999999999,
            chain_id=84532,
            verifying_contract=TOKEN_A,
        )
        assert store.has(
            SIGNER_A,
            _nonce("v2row"),
            chain_id=84532,
            verifying_contract=TOKEN_A,
        )

        # Meta table has the v2 marker.
        cur = store._conn.execute(
            "SELECT value FROM nonce_store_meta WHERE key='schema_version'"
        )
        row = cur.fetchone()
        assert row is not None and int(row[0]) == _SCHEMA_VERSION
    finally:
        store.close()


def test_schema_version_persists_on_reopen(tmp_path):
    """Reopening a fresh v2 file is a no-op migration — rows survive."""
    db = tmp_path / "reopen.db"
    s1 = SqliteNonceStore(str(db))
    s1.add(
        SIGNER_A,
        _nonce("keep"),
        expires_at=9999999999,
        chain_id=1,
        verifying_contract=TOKEN_A,
    )
    s1.close()

    s2 = SqliteNonceStore(str(db))
    try:
        # No legacy-drop warning on a clean reopen.
        assert s2.has(
            SIGNER_A,
            _nonce("keep"),
            chain_id=1,
            verifying_contract=TOKEN_A,
        )
    finally:
        s2.close()
