"""Tests for ``agents.nonce_store``.

Real sqlite3 file I/O — no mocks. The persistence test physically closes
the DB, drops the Python reference, and reopens the same file.
"""

from __future__ import annotations

import os
import sqlite3
import time

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

# Default EIP-712 domain for tests that don't care about a specific chain.
# Phase-4 removed the legacy 2-arg form, so every has()/add() call MUST
# carry a domain — these are the canonical values used across the suite.
DEFAULT_CHAIN_ID = 5042002
DEFAULT_VERIFYING_CONTRACT = "0x3600000000000000000000000000000000000000"


def _nonce(suffix: str) -> str:
    # Pad to 32 bytes hex for realism with EIP-3009 nonces.
    raw = suffix.encode().hex()
    return "0x" + raw.ljust(64, "0")


# ---------------------------------------------------------------------------
# SqliteNonceStore — namespaced (chain_id + verifying_contract required)
# ---------------------------------------------------------------------------


def test_sqlite_basic_add_and_has(tmp_path):
    db = tmp_path / "nonces.db"
    store = SqliteNonceStore(str(db))
    try:
        assert (
            store.has(
                SIGNER_A,
                _nonce("n1"),
                chain_id=DEFAULT_CHAIN_ID,
                verifying_contract=DEFAULT_VERIFYING_CONTRACT,
            )
            is False
        )
        store.add(
            SIGNER_A,
            _nonce("n1"),
            expires_at=int(time.time()) + 600,
            chain_id=DEFAULT_CHAIN_ID,
            verifying_contract=DEFAULT_VERIFYING_CONTRACT,
        )
        assert (
            store.has(
                SIGNER_A,
                _nonce("n1"),
                chain_id=DEFAULT_CHAIN_ID,
                verifying_contract=DEFAULT_VERIFYING_CONTRACT,
            )
            is True
        )
        # Different signer, same nonce — different row.
        assert (
            store.has(
                SIGNER_B,
                _nonce("n1"),
                chain_id=DEFAULT_CHAIN_ID,
                verifying_contract=DEFAULT_VERIFYING_CONTRACT,
            )
            is False
        )
    finally:
        store.close()


def test_sqlite_persists_across_close_and_reopen(tmp_path):
    """The whole point of this slice: nonce survives a restart."""
    db = tmp_path / "nonces.db"
    nonce = _nonce("persist")
    expires = int(time.time()) + 3600

    # 1. Write nonce, close connection.
    store_v1 = SqliteNonceStore(str(db))
    store_v1.add(
        SIGNER_A,
        nonce,
        expires_at=expires,
        chain_id=DEFAULT_CHAIN_ID,
        verifying_contract=DEFAULT_VERIFYING_CONTRACT,
    )
    assert store_v1.has(
        SIGNER_A,
        nonce,
        chain_id=DEFAULT_CHAIN_ID,
        verifying_contract=DEFAULT_VERIFYING_CONTRACT,
    )
    store_v1.close()
    del store_v1

    # 2. File must still exist on disk.
    assert db.exists()
    assert os.path.getsize(str(db)) > 0

    # 3. Reopen the SAME file with a brand-new connection — replay
    # protection must remember the nonce.
    store_v2 = SqliteNonceStore(str(db))
    try:
        assert (
            store_v2.has(
                SIGNER_A,
                nonce,
                chain_id=DEFAULT_CHAIN_ID,
                verifying_contract=DEFAULT_VERIFYING_CONTRACT,
            )
            is True
        )
        # Wrong signer must still be a miss.
        assert (
            store_v2.has(
                SIGNER_B,
                nonce,
                chain_id=DEFAULT_CHAIN_ID,
                verifying_contract=DEFAULT_VERIFYING_CONTRACT,
            )
            is False
        )
    finally:
        store_v2.close()


def test_sqlite_purge_expired_removes_old_rows(tmp_path):
    db = tmp_path / "nonces.db"
    store = SqliteNonceStore(str(db))
    try:
        now = int(time.time())
        dom = dict(
            chain_id=DEFAULT_CHAIN_ID,
            verifying_contract=DEFAULT_VERIFYING_CONTRACT,
        )
        store.add(SIGNER_A, _nonce("old1"), expires_at=now - 100, **dom)
        store.add(SIGNER_A, _nonce("old2"), expires_at=now - 1, **dom)
        store.add(SIGNER_A, _nonce("fresh"), expires_at=now + 1000, **dom)
        assert len(store) == 3

        purged = store.purge_expired(now)
        assert purged == 2
        assert len(store) == 1
        assert store.has(SIGNER_A, _nonce("fresh"), **dom)
        assert not store.has(SIGNER_A, _nonce("old1"), **dom)
        assert not store.has(SIGNER_A, _nonce("old2"), **dom)

        # Idempotent — second purge removes nothing.
        assert store.purge_expired(now) == 0
    finally:
        store.close()


def test_sqlite_duplicate_add_is_idempotent(tmp_path):
    db = tmp_path / "nonces.db"
    store = SqliteNonceStore(str(db))
    try:
        n = _nonce("dup")
        dom = dict(
            chain_id=DEFAULT_CHAIN_ID,
            verifying_contract=DEFAULT_VERIFYING_CONTRACT,
        )
        store.add(SIGNER_A, n, expires_at=1000, **dom)
        # Second add must not throw; ``has`` still True.
        store.add(SIGNER_A, n, expires_at=2000, **dom)
        assert store.has(SIGNER_A, n, **dom)
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
            chain_id=DEFAULT_CHAIN_ID,
            verifying_contract=DEFAULT_VERIFYING_CONTRACT,
        )
        # Same value, different casing must collide (matches ecrecover
        # output which can be checksummed or lowercase).
        assert store.has(
            "0xabcdef0000000000000000000000000000000001",
            "0xdeadbeef",
            chain_id=DEFAULT_CHAIN_ID,
            verifying_contract=DEFAULT_VERIFYING_CONTRACT,
        )
        assert store.has(
            "0xAbCdEf0000000000000000000000000000000001",
            "0xDeAdBeEf",
            chain_id=DEFAULT_CHAIN_ID,
            verifying_contract=DEFAULT_VERIFYING_CONTRACT,
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
        dom = dict(
            chain_id=DEFAULT_CHAIN_ID,
            verifying_contract=DEFAULT_VERIFYING_CONTRACT,
        )
        assert not store.has(SIGNER_A, n, **dom)
        store.add(SIGNER_A, n, expires_at=int(time.time()) + 60, **dom)
        assert store.has(SIGNER_A, n, **dom)
        assert not store.has(SIGNER_B, n, **dom)

        # Purge.
        store.add(SIGNER_B, _nonce("expired"), expires_at=1, **dom)
        purged = store.purge_expired(int(time.time()))
        assert purged == 1
        assert not store.has(SIGNER_B, _nonce("expired"), **dom)
    finally:
        store.close()


def test_inmemory_close_clears_state():
    store = InMemoryNonceStore()
    store.add(
        SIGNER_A,
        _nonce("x"),
        expires_at=9999999999,
        chain_id=DEFAULT_CHAIN_ID,
        verifying_contract=DEFAULT_VERIFYING_CONTRACT,
    )
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
        assert (
            store.has(
                SIGNER_A,
                _nonce("never_added"),
                chain_id=DEFAULT_CHAIN_ID,
                verifying_contract=DEFAULT_VERIFYING_CONTRACT,
            )
            is False
        )
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


@pytest.mark.parametrize("klass", [SqliteNonceStore, InMemoryNonceStore])
def test_legacy_two_arg_form_is_rejected(tmp_path, klass):
    """Phase-4 B2/P0-1: the legacy 2-arg ``has(signer, nonce)`` /
    ``add(signer, nonce, exp)`` form was REMOVED. Omitting the EIP-712
    domain must raise ``ValueError`` at the API boundary — there is no
    silent fallback to the sentinel domain, which is what previously let
    an in-tree caller defeat F2's cross-domain replay protection.

    This asserts the ACTUAL current contract: refuse, don't fall back.
    """
    store = (
        klass(str(tmp_path / "legacy.db"))
        if klass is SqliteNonceStore
        else klass()
    )
    try:
        # 2-arg add() — both domain args omitted → raise.
        with pytest.raises(ValueError):
            store.add(SIGNER_A, _nonce("legacy"), expires_at=9999999999)
        # 2-arg has() — both domain args omitted → raise.
        with pytest.raises(ValueError):
            store.has(SIGNER_A, _nonce("legacy"))
        # Partial domain (only chain_id, no verifying_contract) → still raise.
        with pytest.raises(ValueError):
            store.add(
                SIGNER_A,
                _nonce("legacy"),
                expires_at=9999999999,
                chain_id=DEFAULT_CHAIN_ID,
            )
        with pytest.raises(ValueError):
            store.has(SIGNER_A, _nonce("legacy"), chain_id=DEFAULT_CHAIN_ID)
        # Partial domain (only verifying_contract, no chain_id) → still raise.
        with pytest.raises(ValueError):
            store.has(
                SIGNER_A,
                _nonce("legacy"),
                verifying_contract=DEFAULT_VERIFYING_CONTRACT,
            )
    finally:
        store.close()


def test_sentinel_domain_must_be_opted_into_explicitly(tmp_path):
    """The sentinel ``(0, 0x000…0)`` domain still EXISTS as a partition key,
    but callers can only reach it by passing the ``_LEGACY_*`` values
    explicitly — never by omitting the kwargs.

    A row written under the explicit sentinel domain MUST NOT collide with
    a row written under a real domain (no cross-domain leak), proving the
    namespacing the Phase-4 hardening protects.
    """
    db = tmp_path / "split.db"
    store = SqliteNonceStore(str(db))
    try:
        n = _nonce("split")
        # Explicit sentinel-domain write — the only sanctioned way in.
        store.add(
            SIGNER_A,
            n,
            expires_at=9999999999,
            chain_id=_LEGACY_CHAIN_ID,
            verifying_contract=_LEGACY_VERIFYING_CONTRACT,
        )
        # Real-domain has() must return False — the sentinel row doesn't
        # leak into namespaced lookups.
        assert not store.has(
            SIGNER_A, n, chain_id=1, verifying_contract=TOKEN_A
        )
        # Probing the sentinel domain explicitly finds the row.
        assert store.has(
            SIGNER_A,
            n,
            chain_id=_LEGACY_CHAIN_ID,
            verifying_contract=_LEGACY_VERIFYING_CONTRACT,
        )
        # And the 2-arg form is still refused outright — no implicit
        # sentinel fallback.
        with pytest.raises(ValueError):
            store.has(SIGNER_A, n)
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
