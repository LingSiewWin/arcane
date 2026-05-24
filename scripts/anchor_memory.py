#!/usr/bin/env python3
"""anchor_memory — call MemoryAnchor.anchor(...) for Alice's pinned root.

Phase 4 audit (B5 / N9 / F10): the previous version hard-coded
``ANCHOR_SIG = "anchor(bytes32)"`` so the demo NEVER exercised the
identity-bound entry point introduced by F10 — the on-chain
``MemoryAnchored`` event was always emitted with ``identityId = 0``,
indistinguishable from an unprotected sock-puppet write. We now default
to the identity-bound ``anchor(uint256,bytes32)`` selector and require
the caller to pass an ``identity_id``. The legacy ``anchor(bytes32)``
path remains available behind ``--legacy`` for backwards-compat smoke
tests, but the production demo path must use the identity-bound call.

Usage:
    # Identity-bound (default — F10 path):
    python -m scripts.anchor_memory \
        --rpc-url $RPC \
        --pk $DEPLOYER_PK \
        --anchor 0x<MemoryAnchor address> \
        --identity-id 42 \
        --root 0x<32-byte hex>

    # Legacy (smoke-test only — emits identityId=0):
    python -m scripts.anchor_memory \
        --rpc-url $RPC \
        --pk $DEPLOYER_PK \
        --anchor 0x<MemoryAnchor address> \
        --legacy \
        --root 0x<32-byte hex>

If --root is omitted, the script reads Alice's MemoryService from --memory
(default /tmp/alice.mem) and uses ``pinned_merkle_root()``.

Prints a JSON line:
    {"tx_hash": "...", "root": "...", "anchor": "...", "identity_id": int,
     "path": "identity"|"legacy"}
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from agents.memory_service import MemoryService  # noqa: E402
from scripts.lib.chain import cast_send, wait_for_receipt  # noqa: E402
from scripts.lib.keys import resolve_deployer_key  # noqa: E402


# Phase 4 audit (B5 / N9): default to the identity-bound selector. The
# legacy path is still reachable via ``legacy=True`` for backward-compat
# smoke tests, but the production demo path MUST use the identity-bound
# variant so the on-chain MemoryAnchored event carries a non-zero
# identityId that off-chain observers can audit against the ERC-8004
# identity registry.
IDENTITY_ANCHOR_SIG = "anchor(uint256,bytes32)"
LEGACY_ANCHOR_SIG = "anchor(bytes32)"


def anchor_memory(
    *,
    rpc_url: str,
    pk: str,
    anchor_address: str,
    root_hex: str,
    identity_id: Optional[int] = None,
    legacy: bool = False,
    wait: bool = True,
) -> dict:
    """Submit anchor(...) and (optionally) wait for the receipt.

    Phase 4 audit (B5 / N9 / F10): by default we call the identity-bound
    ``anchor(uint256 identityId, bytes32 root)`` entry point. The caller
    MUST pass ``identity_id`` for the identity-bound path (the contract
    will revert with ``NotIdentityOwner`` if the deployer doesn't own
    that ERC-8004 token). To exercise the legacy path explicitly (for
    smoke tests that don't have an ERC-8004 registry), pass
    ``legacy=True``.

    Returns a dict with ``tx_hash, root, anchor, identity_id, path``,
    plus ``block_number`` and ``event_emitted`` when ``wait=True``.
    """
    if not root_hex.startswith("0x"):
        root_hex = "0x" + root_hex
    if len(root_hex) != 66:
        raise ValueError(f"root must be a 32-byte hex string, got len={len(root_hex)}")

    if legacy:
        # Smoke-test path — emits identityId=0. Documented in the audit
        # as backward-compat only; not for production.
        sig = LEGACY_ANCHOR_SIG
        args = [root_hex]
        path = "legacy"
        resolved_identity_id = 0
    else:
        if identity_id is None:
            raise ValueError(
                "identity_id is required for the F10 identity-bound anchor path. "
                "Pass --identity-id N (or call anchor_memory(identity_id=N)). "
                "If you intentionally want the unprotected legacy path, pass "
                "legacy=True (or --legacy on the CLI) — but the production demo "
                "MUST use the identity-bound variant."
            )
        if identity_id <= 0:
            raise ValueError(
                f"identity_id must be a positive ERC-8004 token id, got {identity_id}; "
                "identityId=0 is the legacy sentinel and must not be used as a "
                "real identity. Pass legacy=True if you mean to call the legacy path."
            )
        sig = IDENTITY_ANCHOR_SIG
        args = [str(int(identity_id)), root_hex]
        path = "identity"
        resolved_identity_id = int(identity_id)

    tx_hash = cast_send(
        rpc_url=rpc_url,
        pk=pk,
        to=anchor_address,
        sig=sig,
        args=args,
    )
    out: dict = {
        "tx_hash": tx_hash,
        "root": root_hex,
        "anchor": anchor_address,
        "identity_id": resolved_identity_id,
        "path": path,
    }

    if wait:
        receipt = wait_for_receipt(rpc_url, tx_hash, timeout=60)
        out["block_number"] = int(receipt.get("blockNumber", "0x0"), 16)
        out["status"] = int(receipt.get("status", "0x0"), 16)
        # MemoryAnchored(address indexed agent, uint256 indexed identityId,
        #                bytes32 root, uint256 timestamp) — F10 signature.
        # Both paths emit the same event; the identity-bound path emits a
        # non-zero identityId in topic[2], legacy emits zero.
        from eth_utils import keccak

        evt_topic = "0x" + keccak(
            b"MemoryAnchored(address,uint256,bytes32,uint256)"
        ).hex()
        logs = receipt.get("logs", []) or []
        out["event_emitted"] = any(
            (lg.get("topics") and lg["topics"][0] == evt_topic) for lg in logs
        )
        # Additionally confirm topic[2] matches the identity we passed
        # — defends against a future contract change quietly emitting
        # identityId=0 on the identity-bound path.
        if path == "identity":
            id_topic_expected = "0x" + format(resolved_identity_id, "064x")
            out["event_identity_id_matches"] = any(
                (
                    lg.get("topics")
                    and lg["topics"][0] == evt_topic
                    and len(lg["topics"]) >= 3
                    and lg["topics"][2].lower() == id_topic_expected.lower()
                )
                for lg in logs
            )
    return out


def _cli() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--rpc-url", required=True)
    p.add_argument(
        "--pk",
        default=None,
        help=(
            "Plain-text deployer private key (fallback). Prefer --account "
            "with an encrypted keystore. Defaults to DEPLOYER_PK env."
        ),
    )
    p.add_argument(
        "--account",
        default=None,
        help=(
            "Encrypted Foundry keystore name in ~/.foundry/keystores/ "
            "(preferred over --pk). Decrypted in-process via eth_account; "
            "password from KEYSTORE_PASSWORD or an interactive prompt."
        ),
    )
    p.add_argument("--anchor", required=True, help="MemoryAnchor contract address")
    p.add_argument("--root", default=None, help="32-byte hex root; default: compute from --memory")
    p.add_argument(
        "--memory",
        default="/tmp/alice.mem",
        help="Path to Alice's MemoryService savefile (used when --root absent)",
    )
    p.add_argument(
        "--identity-id",
        type=int,
        default=None,
        help=(
            "ERC-8004 identity token id to bind the anchor to. Required for the "
            "default F10 path. Omit only when --legacy is passed (smoke-test only)."
        ),
    )
    p.add_argument(
        "--legacy",
        action="store_true",
        help=(
            "Use the legacy anchor(bytes32) entry point (emits identityId=0). "
            "Backward-compat only; production demo path must use the identity-bound "
            "default — pass --identity-id instead."
        ),
    )
    p.add_argument("--no-wait", action="store_true", help="Skip receipt wait")
    args = p.parse_args()

    if args.root:
        root_hex = args.root
    else:
        mem = MemoryService.load(args.memory)
        root_hex = "0x" + mem.pinned_merkle_root().hex()

    # Resolve the key: explicit --pk wins, else keystore account / DEPLOYER_PK
    # via the shared resolver (preferred encrypted-keystore path). The key
    # stays in-process; chain.py signs locally.
    pk = args.pk if args.pk else resolve_deployer_key(
        account=args.account, allow_interactive=True
    )

    result = anchor_memory(
        rpc_url=args.rpc_url,
        pk=pk,
        anchor_address=args.anchor,
        root_hex=root_hex,
        identity_id=args.identity_id,
        legacy=args.legacy,
        wait=not args.no_wait,
    )
    print(json.dumps(result))


if __name__ == "__main__":
    _cli()
