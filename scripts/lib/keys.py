"""keys.py — deployer private-key resolution for AgoraHack.

SECURITY (matches Circle's ``use-arc.md``: "prefer encrypted keystores or
interactive import over plain-text keys"):

The hardest/safest key-handling path is an encrypted Foundry keystore. A
keystore JSON file is an scrypt/pbkdf2-encrypted private key — the raw key
only ever exists in memory after an explicit decrypt with the operator's
password. This module resolves a deployer private key in priority order:

  1. ``--account <name>`` / ``DEPLOYER_ACCOUNT`` env
     -> load ``~/.foundry/keystores/<name>`` and decrypt with
        ``eth_account.Account.decrypt``. Password comes from
        ``KEYSTORE_PASSWORD`` env OR an interactive ``getpass`` prompt.
        The password is NEVER echoed, NEVER logged.

  2. ``DEPLOYER_PK`` env -> use directly (existing behaviour, kept as a
     fallback).

  3. Neither -> raise a clear error explaining both options.

Once resolved, the key string is handed to ``scripts.lib.chain`` exactly as
before: chain.py signs in-process via ``eth_account`` and broadcasts the raw
tx. The key never reaches argv and never enters a child process env. This
module ONLY changes WHERE the key comes from — the in-process signing
contract is unchanged.

NEVER log the resolved key or the password. The functions here return the
key string; the caller is responsible for keeping it in-process.
"""

from __future__ import annotations

import getpass
import json
import os
from pathlib import Path
from typing import Optional


DEFAULT_KEYSTORE_DIR = Path.home() / ".foundry" / "keystores"


class KeyResolutionError(RuntimeError):
    """Raised when no deployer key can be resolved.

    Carries a message that explains BOTH supported paths (keystore account
    and ``DEPLOYER_PK``) so the operator knows how to proceed. NEVER contains
    a key or password.
    """


def keystore_path(account: str, *, keystore_dir: Optional[Path] = None) -> Path:
    """Return the path to ``<keystore_dir>/<account>``.

    Foundry's ``cast wallet import <name> --interactive`` writes the
    encrypted keystore JSON to ``~/.foundry/keystores/<name>`` (no extension).
    ``keystore_dir`` is overridable for tests; production uses
    ``~/.foundry/keystores``.
    """
    base = keystore_dir if keystore_dir is not None else DEFAULT_KEYSTORE_DIR
    return Path(base) / account


def _decrypt_keystore_file(
    path: Path,
    *,
    password: Optional[str],
    interactive_prompt: bool,
) -> str:
    """Decrypt the keystore JSON at ``path`` and return a 0x-prefixed key.

    Password resolution: ``KEYSTORE_PASSWORD`` (passed in as ``password``)
    takes precedence; otherwise — only if ``interactive_prompt`` is True and
    a TTY is available — prompt via ``getpass`` (which never echoes). The
    password is used solely as an argument to ``eth_account.Account.decrypt``
    and is never logged or returned.
    """
    if not path.exists():
        raise KeyResolutionError(
            f"keystore not found at {path}. Create one with:\n"
            f"    cast wallet import {path.name} --interactive\n"
            f"(writes an encrypted keystore to {path.parent})."
        )

    try:
        keystore_json = json.loads(path.read_text())
    except (OSError, ValueError) as e:
        raise KeyResolutionError(
            f"keystore at {path} is not valid JSON: {type(e).__name__}"
        ) from e

    if password is None:
        if not interactive_prompt:
            raise KeyResolutionError(
                f"keystore {path.name} found but no password available. "
                "Set KEYSTORE_PASSWORD or run interactively so you can be "
                "prompted (the password is never echoed or logged)."
            )
        # getpass reads from the controlling TTY without echoing. If stdin is
        # not a TTY (CI / piped), getpass falls back to a warning + stdin
        # read; we surface a clear error instead of silently reading an
        # empty password.
        try:
            password = getpass.getpass(
                f"Password for keystore '{path.name}': "
            )
        except (EOFError, getpass.GetPassWarning) as e:  # type: ignore[attr-defined]
            raise KeyResolutionError(
                f"could not read keystore password interactively for "
                f"'{path.name}': {type(e).__name__}. Set KEYSTORE_PASSWORD "
                "instead."
            ) from e
        if not password:
            raise KeyResolutionError(
                f"empty password entered for keystore '{path.name}'."
            )

    from eth_account import Account

    try:
        key_bytes = Account.decrypt(keystore_json, password)
    except Exception as e:  # noqa: BLE001 - eth_account raises many types
        # Do NOT include the password or any key material in the error.
        # A bad password raises ValueError("MAC mismatch") from eth_account.
        raise KeyResolutionError(
            f"failed to decrypt keystore '{path.name}': {type(e).__name__} "
            "(wrong password, or corrupt keystore). The password is never "
            "logged."
        ) from e

    # eth_account.Account.decrypt returns raw 32-byte private key bytes
    # (HexBytes). Normalise to a 0x-prefixed hex string for chain.py.
    return "0x" + bytes(key_bytes).hex()


def resolve_deployer_key(
    *,
    account: Optional[str] = None,
    keystore_dir: Optional[Path] = None,
    allow_interactive: bool = True,
) -> str:
    """Resolve a deployer private key, returned as a 0x-prefixed hex string.

    Priority order:

      1. Keystore account: ``account`` arg OR ``DEPLOYER_ACCOUNT`` env. The
         keystore is loaded from ``~/.foundry/keystores/<name>`` and decrypted
         with ``eth_account.Account.decrypt`` using ``KEYSTORE_PASSWORD`` OR
         an interactive ``getpass`` prompt.
      2. ``DEPLOYER_PK`` env: used directly (legacy fallback).
      3. Neither: raise ``KeyResolutionError`` explaining both options.

    SECURITY: the returned key stays in the caller's process; chain.py signs
    locally. The password is never echoed, never logged, never returned.
    ``allow_interactive`` lets non-interactive contexts (preflight, tests)
    disable the ``getpass`` prompt and rely solely on ``KEYSTORE_PASSWORD``.

    ``account``/``keystore_dir`` are explicit overrides for tests and the CLI;
    in production the env vars + default keystore dir are used.
    """
    resolved_account = account or os.environ.get("DEPLOYER_ACCOUNT", "").strip()

    # --- Path 1: encrypted keystore account (preferred) -------------------
    if resolved_account:
        path = keystore_path(resolved_account, keystore_dir=keystore_dir)
        password = os.environ.get("KEYSTORE_PASSWORD")
        # An explicitly-set-but-empty KEYSTORE_PASSWORD should not be treated
        # as "no password" silently — but eth_account would just fail the MAC
        # check, which is fine. We pass it through as-is (None means unset).
        return _decrypt_keystore_file(
            path,
            password=password,
            interactive_prompt=allow_interactive,
        )

    # --- Path 2: DEPLOYER_PK env (legacy fallback) ------------------------
    pk = os.environ.get("DEPLOYER_PK", "").strip()
    if pk:
        return pk

    # --- Path 3: neither ---------------------------------------------------
    raise KeyResolutionError(
        "no deployer key available. Provide one of:\n"
        "  (preferred) an encrypted Foundry keystore:\n"
        "      cast wallet import deployer --interactive\n"
        "      then pass --account deployer (or set DEPLOYER_ACCOUNT=deployer),\n"
        "      with the password in KEYSTORE_PASSWORD or entered when prompted.\n"
        "  (fallback) a plain-text private key:\n"
        "      export DEPLOYER_PK=0x<64-hex-chars>\n"
        "Circle's use-arc guidance prefers the keystore path for live broadcast."
    )


def can_resolve_deployer_key(
    *,
    account: Optional[str] = None,
    keystore_dir: Optional[Path] = None,
) -> bool:
    """Non-interactive probe: True iff a key COULD be resolved right now.

    Used by preflight to go GREEN without actually decrypting (and without
    prompting). A keystore account counts as resolvable when the keystore
    file exists AND a password source is available (``KEYSTORE_PASSWORD``),
    OR when ``DEPLOYER_PK`` is set. We do NOT prompt and do NOT decrypt here —
    this is a cheap pre-broadcast readiness check.
    """
    resolved_account = account or os.environ.get("DEPLOYER_ACCOUNT", "").strip()
    if resolved_account:
        path = keystore_path(resolved_account, keystore_dir=keystore_dir)
        # The file must exist; a password must be available non-interactively.
        if path.exists() and os.environ.get("KEYSTORE_PASSWORD") is not None:
            return True
        # Even without KEYSTORE_PASSWORD, an existing keystore is resolvable
        # interactively at broadcast time — but for the non-interactive
        # readiness probe we only assert GREEN when we KNOW it will work
        # without a human. Existence alone is reported via the caller.
        return path.exists() and os.environ.get("KEYSTORE_PASSWORD") is not None

    return bool(os.environ.get("DEPLOYER_PK", "").strip())
