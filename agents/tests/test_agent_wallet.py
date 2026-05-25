"""Tests for agents.agent_wallet — the encrypted agent keypair vault.

No network, no torch. Uses a tmp keystore dir and an explicit password so
nothing touches the real ``agents/.arena_keystore/`` vault.
"""

from __future__ import annotations

import json

import pytest
from eth_account import Account

from agents.agent_wallet import (
    AgentKeyError,
    AgentWallet,
    load_agent_wallets,
    spawn_keypairs,
)

PW = "testpw"


def test_spawn_writes_keystores(tmp_path):
    wallets = spawn_keypairs(3, password=PW, keystore_dir=tmp_path)

    assert len(wallets) == 3
    keystore_files = list(tmp_path.glob("*.keystore"))
    assert len(keystore_files) == 3

    # Each keystore is encrypted Secret Storage JSON, NOT a plaintext key.
    for f in keystore_files:
        doc = json.loads(f.read_text())
        assert "crypto" in doc or "Crypto" in doc
        assert "address" in doc

    # Returned wallets carry 0x-prefixed hex private keys.
    for w in wallets:
        assert w.private_key.startswith("0x")
        assert len(w.private_key) == 66


def test_round_trip_addresses_match(tmp_path):
    spawned = spawn_keypairs(3, password=PW, keystore_dir=tmp_path)
    loaded = load_agent_wallets(password=PW, keystore_dir=tmp_path)

    assert len(loaded) == 3

    spawned_addrs = {w.address.lower() for w in spawned}
    loaded_addrs = {w.address.lower() for w in loaded}
    assert spawned_addrs == loaded_addrs


def test_loaded_keys_derive_matching_addresses(tmp_path):
    spawn_keypairs(2, password=PW, keystore_dir=tmp_path)
    loaded = load_agent_wallets(password=PW, keystore_dir=tmp_path)

    for w in loaded:
        derived = Account.from_key(w.private_key).address
        assert derived == w.address


def test_load_is_sorted_deterministically(tmp_path):
    spawn_keypairs(5, password=PW, keystore_dir=tmp_path)
    loaded = load_agent_wallets(password=PW, keystore_dir=tmp_path)

    addrs = [w.address.lower() for w in loaded]
    assert addrs == sorted(addrs)


def test_wrong_password_fails(tmp_path):
    spawn_keypairs(1, password=PW, keystore_dir=tmp_path)
    with pytest.raises(AgentKeyError):
        load_agent_wallets(password="wrongpw", keystore_dir=tmp_path)


def test_password_from_env(tmp_path, monkeypatch):
    monkeypatch.delenv("KEYSTORE_PASSWORD", raising=False)
    monkeypatch.setenv("ARENA_KEY_PASSWORD", "envpw")

    spawn_keypairs(1, keystore_dir=tmp_path)
    loaded = load_agent_wallets(keystore_dir=tmp_path)
    assert len(loaded) == 1


def test_missing_password_raises(tmp_path, monkeypatch):
    monkeypatch.delenv("ARENA_KEY_PASSWORD", raising=False)
    monkeypatch.delenv("KEYSTORE_PASSWORD", raising=False)
    with pytest.raises(AgentKeyError):
        spawn_keypairs(1, keystore_dir=tmp_path)


def test_repr_redacts_private_key():
    w = AgentWallet(address="0xabc", private_key="0xdeadbeef", identity_id=7)
    text = repr(w)
    assert "deadbeef" not in text
    assert "redacted" in text
    assert "0xabc" in text
