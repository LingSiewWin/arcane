"""Tests for encrypted-keystore deployer-key resolution.

These use a REAL ``eth_account`` encrypt/decrypt round-trip — no mocks. We
encrypt a known private key into a keystore JSON, point the resolver at a
temp keystore dir, and assert it decrypts back to the exact same key/address.

Security assertions:
  * the password and raw key NEVER appear in captured stdout/stderr;
  * the resolver refuses cleanly when no key source is configured.
"""

from __future__ import annotations

import io
import json
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from eth_account import Account  # noqa: E402

from scripts.lib.keys import (  # noqa: E402
    KeyResolutionError,
    can_resolve_deployer_key,
    resolve_deployer_key,
)
from scripts.preflight_checks import (  # noqa: E402
    Severity,
    check_deployer_key,
)


# A deterministic, well-known test key (anvil account #1). Using a published
# test key here is intentional — there is nothing secret about it, and it lets
# us assert an exact derived address.
KNOWN_KEY = "0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d"
KNOWN_ADDR = "0x70997970C51812dc3A010C7d01b50e0d17dc79C8"
KEYSTORE_PW = "correct horse battery staple"


def _write_keystore(dir_path: Path, name: str, key: str, pw: str) -> Path:
    """Encrypt ``key`` under ``pw`` and write the keystore JSON to dir/name."""
    keystore = Account.encrypt(key, pw)
    path = dir_path / name
    path.write_text(json.dumps(keystore))
    return path


# ---------------------------------------------------------------------------
# Path 2: DEPLOYER_PK fallback
# ---------------------------------------------------------------------------


def test_resolve_key_from_env_pk(monkeypatch):
    """DEPLOYER_PK set (and no account) -> returned verbatim."""
    monkeypatch.delenv("DEPLOYER_ACCOUNT", raising=False)
    monkeypatch.delenv("KEYSTORE_PASSWORD", raising=False)
    monkeypatch.setenv("DEPLOYER_PK", KNOWN_KEY)

    resolved = resolve_deployer_key()
    assert resolved == KNOWN_KEY
    # And it derives the expected address.
    assert Account.from_key(resolved).address == KNOWN_ADDR


# ---------------------------------------------------------------------------
# Path 1: encrypted keystore (real eth_account round-trip)
# ---------------------------------------------------------------------------


def test_resolve_key_from_keystore(monkeypatch, tmp_path):
    """Real encrypt -> write -> resolve -> decrypt round-trip.

    No DEPLOYER_PK in env; the key comes solely from the encrypted keystore,
    decrypted with KEYSTORE_PASSWORD. Asserts the decrypted key + derived
    address exactly match the known inputs.
    """
    monkeypatch.delenv("DEPLOYER_PK", raising=False)
    ks_dir = tmp_path / "keystores"
    ks_dir.mkdir()
    _write_keystore(ks_dir, "deployer", KNOWN_KEY, KEYSTORE_PW)

    monkeypatch.setenv("KEYSTORE_PASSWORD", KEYSTORE_PW)
    monkeypatch.setenv("DEPLOYER_ACCOUNT", "deployer")

    resolved = resolve_deployer_key(keystore_dir=ks_dir, allow_interactive=False)

    assert resolved.lower() == KNOWN_KEY.lower()
    assert Account.from_key(resolved).address == KNOWN_ADDR


def test_resolve_key_keystore_takes_priority_over_env_pk(monkeypatch, tmp_path):
    """When BOTH a keystore account and DEPLOYER_PK are set, keystore wins.

    The keystore holds KNOWN_KEY; DEPLOYER_PK holds a DIFFERENT key. The
    resolver must return the keystore key (preferred path).
    """
    other_key = "0x" + "11" * 32
    monkeypatch.setenv("DEPLOYER_PK", other_key)
    ks_dir = tmp_path / "keystores"
    ks_dir.mkdir()
    _write_keystore(ks_dir, "deployer", KNOWN_KEY, KEYSTORE_PW)
    monkeypatch.setenv("KEYSTORE_PASSWORD", KEYSTORE_PW)
    monkeypatch.setenv("DEPLOYER_ACCOUNT", "deployer")

    resolved = resolve_deployer_key(keystore_dir=ks_dir, allow_interactive=False)
    assert resolved.lower() == KNOWN_KEY.lower()
    assert resolved.lower() != other_key.lower()


def test_resolve_key_wrong_password_raises(monkeypatch, tmp_path):
    """A wrong KEYSTORE_PASSWORD raises KeyResolutionError, not the raw key."""
    monkeypatch.delenv("DEPLOYER_PK", raising=False)
    ks_dir = tmp_path / "keystores"
    ks_dir.mkdir()
    _write_keystore(ks_dir, "deployer", KNOWN_KEY, KEYSTORE_PW)
    monkeypatch.setenv("KEYSTORE_PASSWORD", "wrong-password")
    monkeypatch.setenv("DEPLOYER_ACCOUNT", "deployer")

    with pytest.raises(KeyResolutionError) as ei:
        resolve_deployer_key(keystore_dir=ks_dir, allow_interactive=False)
    # Error must NOT contain the real key or the correct password.
    msg = str(ei.value)
    assert KNOWN_KEY[2:] not in msg
    assert KEYSTORE_PW not in msg


# ---------------------------------------------------------------------------
# Path 3: neither set
# ---------------------------------------------------------------------------


def test_resolve_key_neither_set_raises(monkeypatch):
    """No DEPLOYER_ACCOUNT and no DEPLOYER_PK -> clear KeyResolutionError."""
    monkeypatch.delenv("DEPLOYER_ACCOUNT", raising=False)
    monkeypatch.delenv("DEPLOYER_PK", raising=False)
    monkeypatch.delenv("KEYSTORE_PASSWORD", raising=False)

    with pytest.raises(KeyResolutionError) as ei:
        resolve_deployer_key()
    msg = str(ei.value)
    # The error must explain BOTH options.
    assert "keystore" in msg.lower()
    assert "DEPLOYER_PK" in msg


def test_resolve_key_missing_keystore_file_raises(monkeypatch, tmp_path):
    """DEPLOYER_ACCOUNT names a keystore that doesn't exist -> clear error."""
    monkeypatch.delenv("DEPLOYER_PK", raising=False)
    monkeypatch.setenv("KEYSTORE_PASSWORD", KEYSTORE_PW)
    monkeypatch.setenv("DEPLOYER_ACCOUNT", "ghost")

    with pytest.raises(KeyResolutionError) as ei:
        resolve_deployer_key(keystore_dir=tmp_path, allow_interactive=False)
    assert "not found" in str(ei.value).lower()


# ---------------------------------------------------------------------------
# Preflight goes GREEN with a keystore (no DEPLOYER_PK)
# ---------------------------------------------------------------------------


def test_preflight_passes_with_keystore(monkeypatch, tmp_path):
    """check_deployer_key() is GREEN when a keystore + password are present
    even though DEPLOYER_PK is unset."""
    monkeypatch.delenv("DEPLOYER_PK", raising=False)
    ks_dir = tmp_path / ".foundry" / "keystores"
    ks_dir.mkdir(parents=True)
    _write_keystore(ks_dir, "deployer", KNOWN_KEY, KEYSTORE_PW)
    # Point the default keystore dir (HOME/.foundry/keystores) at our temp dir.
    monkeypatch.setenv("HOME", str(tmp_path))
    # keys.DEFAULT_KEYSTORE_DIR was bound at import time from Path.home(); the
    # check uses keystore_path() which reads DEFAULT_KEYSTORE_DIR. Patch it.
    import scripts.lib.keys as keys_mod

    monkeypatch.setattr(keys_mod, "DEFAULT_KEYSTORE_DIR", ks_dir)

    monkeypatch.setenv("KEYSTORE_PASSWORD", KEYSTORE_PW)
    monkeypatch.setenv("DEPLOYER_ACCOUNT", "deployer")

    r = check_deployer_key()
    assert r.severity is Severity.GREEN, f"got {r.severity}: {r.message}"
    assert r.evidence.get("source") == "keystore"
    # Never leak the key.
    assert KNOWN_KEY[2:] not in json.dumps(r.evidence)


def test_preflight_deployer_key_still_green_with_env_pk(monkeypatch):
    """Regression: the DEPLOYER_PK path still goes GREEN (no keystore)."""
    monkeypatch.delenv("DEPLOYER_ACCOUNT", raising=False)
    monkeypatch.delenv("KEYSTORE_PASSWORD", raising=False)
    monkeypatch.setenv("DEPLOYER_PK", "0x" + "a" * 64)
    r = check_deployer_key()
    assert r.severity is Severity.GREEN
    assert r.evidence.get("source") == "env_pk"
    # Only the 6-char prefix may appear, never the full key.
    assert "aaaaaa" not in json.dumps(r.evidence)


def test_preflight_deployer_key_red_when_neither(monkeypatch):
    monkeypatch.delenv("DEPLOYER_ACCOUNT", raising=False)
    monkeypatch.delenv("DEPLOYER_PK", raising=False)
    monkeypatch.delenv("KEYSTORE_PASSWORD", raising=False)
    r = check_deployer_key()
    assert r.severity is Severity.RED
    assert "DEPLOYER_PK" in (r.next_step or "")


def test_can_resolve_deployer_key_probe(monkeypatch, tmp_path):
    """can_resolve_deployer_key() is a non-interactive readiness probe."""
    monkeypatch.delenv("DEPLOYER_PK", raising=False)
    monkeypatch.delenv("DEPLOYER_ACCOUNT", raising=False)
    monkeypatch.delenv("KEYSTORE_PASSWORD", raising=False)
    assert can_resolve_deployer_key() is False

    monkeypatch.setenv("DEPLOYER_PK", KNOWN_KEY)
    assert can_resolve_deployer_key() is True
    monkeypatch.delenv("DEPLOYER_PK")

    ks_dir = tmp_path / "keystores"
    ks_dir.mkdir()
    _write_keystore(ks_dir, "deployer", KNOWN_KEY, KEYSTORE_PW)
    monkeypatch.setenv("DEPLOYER_ACCOUNT", "deployer")
    # Without a password it's not resolvable non-interactively.
    assert can_resolve_deployer_key(keystore_dir=ks_dir) is False
    monkeypatch.setenv("KEYSTORE_PASSWORD", KEYSTORE_PW)
    assert can_resolve_deployer_key(keystore_dir=ks_dir) is True


# ---------------------------------------------------------------------------
# The password + raw key must NEVER be logged
# ---------------------------------------------------------------------------


def test_keystore_password_never_logged(monkeypatch, tmp_path):
    """Capture stdout+stderr across a full resolution and assert neither the
    password nor the decrypted key ever appears."""
    monkeypatch.delenv("DEPLOYER_PK", raising=False)
    ks_dir = tmp_path / "keystores"
    ks_dir.mkdir()
    _write_keystore(ks_dir, "deployer", KNOWN_KEY, KEYSTORE_PW)
    monkeypatch.setenv("KEYSTORE_PASSWORD", KEYSTORE_PW)
    monkeypatch.setenv("DEPLOYER_ACCOUNT", "deployer")

    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        resolved = resolve_deployer_key(keystore_dir=ks_dir, allow_interactive=False)

    captured = out.getvalue() + err.getvalue()
    # Sanity: we actually resolved the right key.
    assert resolved.lower() == KNOWN_KEY.lower()
    # Nothing sensitive leaked to stdout/stderr.
    assert KEYSTORE_PW not in captured
    assert KNOWN_KEY not in captured
    assert KNOWN_KEY[2:] not in captured
    assert resolved not in captured
    assert resolved[2:] not in captured


def test_keystore_password_never_logged_on_failure(monkeypatch, tmp_path):
    """Even on a wrong-password failure, nothing sensitive is printed."""
    monkeypatch.delenv("DEPLOYER_PK", raising=False)
    ks_dir = tmp_path / "keystores"
    ks_dir.mkdir()
    _write_keystore(ks_dir, "deployer", KNOWN_KEY, KEYSTORE_PW)
    bad_pw = "definitely-not-the-password"
    monkeypatch.setenv("KEYSTORE_PASSWORD", bad_pw)
    monkeypatch.setenv("DEPLOYER_ACCOUNT", "deployer")

    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        with pytest.raises(KeyResolutionError) as ei:
            resolve_deployer_key(keystore_dir=ks_dir, allow_interactive=False)

    captured = out.getvalue() + err.getvalue() + str(ei.value)
    assert bad_pw not in captured
    assert KEYSTORE_PW not in captured
    assert KNOWN_KEY[2:] not in captured
