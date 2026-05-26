"""agent_wallet.py — autonomous-agent keypair vault for the arena.

Spawned on-chain agents each need their own EVM keypair. Those keys are
secrets: they must NEVER be printed, logged, committed, or passed on argv.
This module follows the same in-process / encrypted-keystore contract as
``scripts/lib/keys.py`` and ``scripts/lib/chain.py``:

  * Keys are generated locally via ``eth_account.Account.create()`` — the
    raw private key only ever lives in this Python process's memory.
  * Each keypair is persisted as an ENCRYPTED keystore JSON (the Web3
    Secret Storage / eth_account format, scrypt-encrypted under a password).
    The plaintext key is never written to disk.
  * Keystores land in a GITIGNORED directory (default
    ``agents/.arena_keystore/``, covered by the root ``*.keystore`` rule).
  * A round-trip loader decrypts the keystores back into memory for use by
    agent runners.

SECURITY: this module returns ``AgentWallet`` objects that carry the raw
private key in memory for the caller to sign with. It NEVER logs the key or
the password, and NEVER places either in argv or a child-process env. The
``__repr__`` of ``AgentWallet`` is overridden to redact the key so an
accidental ``print(wallet)`` / log line cannot leak it.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence, Union

from eth_account import Account


# Default vault dir: repo-root ``agents/.arena_keystore/``. Resolved relative
# to this file so it is correct regardless of the caller's cwd. Covered by the
# root ``.gitignore`` ``*.keystore`` pattern (verified via ``git check-ignore``).
DEFAULT_KEYSTORE_DIR = Path(__file__).resolve().parent / ".arena_keystore"

# Env vars consulted (in order) when no explicit password is passed. Mirrors
# the ``KEYSTORE_PASSWORD`` convention used by ``scripts/lib/keys.py``, with an
# arena-specific override taking precedence.
_PASSWORD_ENV_VARS = ("ARENA_KEY_PASSWORD", "KEYSTORE_PASSWORD")

# Non-secret sidecar mapping ``checksummed address -> ERC-8004 identity_id``.
# The encrypted keystores hold ONLY the key material (Secret Storage format) and
# carry no identity id, so a reused pool would lose the minted identity_ids that
# the per-agent ``anchor_fn`` needs. We persist them next to the keystores in a
# plaintext JSON — it contains NO secret (the identity id is public on-chain).
IDENTITIES_FILENAME = "identities.json"


class AgentKeyError(RuntimeError):
    """Raised on vault errors (no password, decrypt failure, bad keystore).

    Carries an explanatory message that NEVER contains a key or password.
    """


@dataclass
class AgentWallet:
    """An autonomous agent's EVM keypair.

    ``private_key`` is a secret held in memory only. Its value is redacted
    from ``repr`` so an accidental log/print cannot leak it.
    """

    address: str
    private_key: str
    identity_id: Optional[int] = None

    def __repr__(self) -> str:  # noqa: D105 - redact the secret
        return (
            f"AgentWallet(address={self.address!r}, "
            f"private_key='0x<redacted>', identity_id={self.identity_id!r})"
        )


def _resolve_password(password: Optional[str]) -> str:
    """Return the keystore password, or raise a clear error if none available.

    Priority: explicit ``password`` arg, then ``ARENA_KEY_PASSWORD``, then
    ``KEYSTORE_PASSWORD``. The password is never logged or echoed.
    """
    if password is not None:
        if password == "":
            raise AgentKeyError("empty keystore password is not allowed.")
        return password

    for var in _PASSWORD_ENV_VARS:
        env_val = os.environ.get(var)
        if env_val:
            return env_val

    raise AgentKeyError(
        "no keystore password available. Pass password=... or set one of "
        f"{', '.join(_PASSWORD_ENV_VARS)} (the password is never logged)."
    )


def _resolve_keystore_dir(keystore_dir: Optional[Union[str, Path]]) -> Path:
    """Return the keystore dir as a ``Path``, defaulting to the arena vault."""
    base = Path(keystore_dir) if keystore_dir is not None else DEFAULT_KEYSTORE_DIR
    return base


def spawn_keypairs(
    n: int,
    *,
    password: Optional[str] = None,
    keystore_dir: Optional[Union[str, Path]] = None,
) -> list[AgentWallet]:
    """Generate ``n`` agent keypairs and persist each as an encrypted keystore.

    For each agent: create a fresh key via ``Account.create()``, encrypt it
    with ``Account.encrypt(key, password)``, and write the keystore JSON to
    ``<keystore_dir>/<address>.keystore``. The directory is created if missing.

    Returns the list of ``AgentWallet`` (address + 0x-prefixed hex private key)
    for immediate in-process use. The plaintext key is never written to disk.

    SECURITY: keys stay in this process. Nothing is logged. ``keystore_dir``
    defaults to the gitignored ``agents/.arena_keystore/``.
    """
    if n < 0:
        raise ValueError(f"n must be non-negative, got {n}")

    pw = _resolve_password(password)
    ks_dir = _resolve_keystore_dir(keystore_dir)
    ks_dir.mkdir(parents=True, exist_ok=True)

    wallets: list[AgentWallet] = []
    for _ in range(n):
        acct = Account.create()
        # Account.encrypt returns a dict (Secret Storage JSON). scrypt-encrypted
        # under ``pw``; the plaintext key is not present in this structure.
        keystore = Account.encrypt(acct.key, pw)
        out_path = ks_dir / f"{acct.address}.keystore"
        # Restrictive perms: keystore is encrypted, but defence-in-depth.
        out_path.write_text(json.dumps(keystore))
        try:
            out_path.chmod(0o600)
        except OSError:
            # chmod may be unsupported on some filesystems; the keystore is
            # encrypted regardless, so this is best-effort.
            pass
        wallets.append(
            AgentWallet(
                address=acct.address,
                private_key="0x" + bytes(acct.key).hex(),
            )
        )

    return wallets


def save_identities(
    wallets: Sequence[AgentWallet],
    *,
    keystore_dir: Optional[Union[str, Path]] = None,
) -> Path:
    """Persist the ``address -> identity_id`` map for a provisioned pool.

    Writes (or merges into) ``<keystore_dir>/identities.json`` so a later REUSE
    of this pool can recover each agent's minted ERC-8004 identity id (the
    encrypted keystores do not carry it). Only wallets with a non-``None``
    ``identity_id`` are recorded. The file holds NO secret — the identity id is
    public on-chain — so it is written plaintext (not gitignored material).

    Returns the path written. Existing entries for addresses not in ``wallets``
    are preserved (merge, not overwrite).
    """
    ks_dir = _resolve_keystore_dir(keystore_dir)
    ks_dir.mkdir(parents=True, exist_ok=True)
    path = ks_dir / IDENTITIES_FILENAME

    mapping: dict[str, int] = {}
    if path.exists():
        try:
            existing = json.loads(path.read_text())
            if isinstance(existing, dict):
                mapping.update({str(k): int(v) for k, v in existing.items()})
        except (OSError, ValueError, TypeError):
            # A corrupt sidecar is non-fatal: we just rebuild it from `wallets`.
            mapping = {}

    for w in wallets:
        if w.identity_id is not None:
            mapping[w.address] = int(w.identity_id)

    path.write_text(json.dumps(mapping, indent=2, sort_keys=True))
    return path


def _load_identities(ks_dir: Path) -> dict[str, int]:
    """Read the ``identities.json`` sidecar (address -> identity_id), if present.

    Returns an empty dict when the sidecar is absent or malformed. Keys are
    lowercased for case-insensitive address matching.
    """
    path = ks_dir / IDENTITIES_FILENAME
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, int] = {}
    for k, v in raw.items():
        try:
            out[str(k).lower()] = int(v)
        except (TypeError, ValueError):
            continue
    return out


def load_agent_wallets(
    *,
    password: Optional[str] = None,
    keystore_dir: Optional[Union[str, Path]] = None,
) -> list[AgentWallet]:
    """Decrypt every ``*.keystore`` in ``keystore_dir`` back into wallets.

    Reads each keystore JSON, decrypts via ``Account.decrypt(json, password)``
    to recover the private key, and derives the address with
    ``Account.from_key(pk).address``. If an ``identities.json`` sidecar is present
    (written by ``save_identities`` at provision time), each wallet's
    ``identity_id`` is populated from it — so a REUSED pool keeps the minted
    ERC-8004 identity the per-agent ``anchor_fn`` needs. Returns the wallets
    sorted by checksummed address for determinism.

    Raises ``AgentKeyError`` if the dir is missing, a keystore is malformed, or
    the password is wrong (the password is never logged).
    """
    pw = _resolve_password(password)
    ks_dir = _resolve_keystore_dir(keystore_dir)

    if not ks_dir.exists():
        raise AgentKeyError(f"keystore dir not found: {ks_dir}")

    identities = _load_identities(ks_dir)

    wallets: list[AgentWallet] = []
    for path in sorted(ks_dir.glob("*.keystore")):
        try:
            keystore_json = json.loads(path.read_text())
        except (OSError, ValueError) as e:
            raise AgentKeyError(
                f"keystore {path.name} is not valid JSON: {type(e).__name__}"
            ) from e

        try:
            key_bytes = Account.decrypt(keystore_json, pw)
        except Exception as e:  # noqa: BLE001 - eth_account raises many types
            # Never include the password or any key material in the error.
            # A wrong password raises ValueError("MAC mismatch").
            raise AgentKeyError(
                f"failed to decrypt keystore '{path.name}': {type(e).__name__} "
                "(wrong password, or corrupt keystore). The password is never "
                "logged."
            ) from e

        pk_hex = "0x" + bytes(key_bytes).hex()
        address = Account.from_key(pk_hex).address
        wallets.append(
            AgentWallet(
                address=address,
                private_key=pk_hex,
                identity_id=identities.get(address.lower()),
            )
        )

    wallets.sort(key=lambda w: w.address.lower())
    return wallets
