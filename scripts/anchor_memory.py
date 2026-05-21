#!/usr/bin/env python3
"""anchor_memory — call MemoryAnchor.anchor(bytes32 root) for Alice's pinned root.

Usage:
    python -m scripts.anchor_memory \
        --rpc-url $RPC \
        --pk $DEPLOYER_PK \
        --anchor 0x<MemoryAnchor address> \
        --root 0x<32-byte hex>

If --root is omitted, the script reads Alice's MemoryService from --memory
(default /tmp/alice.mem) and uses ``pinned_merkle_root()``.

Prints a JSON line: {"tx_hash": "...", "root": "...", "anchor": "..."}
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from agents.memory_service import MemoryService  # noqa: E402
from scripts.lib.chain import cast_send, wait_for_receipt  # noqa: E402


ANCHOR_SIG = "anchor(bytes32)"


def anchor_memory(
    *,
    rpc_url: str,
    pk: str,
    anchor_address: str,
    root_hex: str,
    wait: bool = True,
) -> dict:
    """Submit anchor(root) and (optionally) wait for the receipt.

    Returns a dict with tx_hash, root, anchor address, block_number (if waited),
    and whether the MemoryAnchored event was observed.
    """
    if not root_hex.startswith("0x"):
        root_hex = "0x" + root_hex
    if len(root_hex) != 66:
        raise ValueError(f"root must be a 32-byte hex string, got len={len(root_hex)}")

    tx_hash = cast_send(
        rpc_url=rpc_url,
        pk=pk,
        to=anchor_address,
        sig=ANCHOR_SIG,
        args=[root_hex],
    )
    out: dict = {"tx_hash": tx_hash, "root": root_hex, "anchor": anchor_address}

    if wait:
        receipt = wait_for_receipt(rpc_url, tx_hash, timeout=60)
        out["block_number"] = int(receipt.get("blockNumber", "0x0"), 16)
        out["status"] = int(receipt.get("status", "0x0"), 16)
        # MemoryAnchored(address indexed agent, bytes32 root, uint256 timestamp)
        # topic0 = keccak256("MemoryAnchored(address,bytes32,uint256)")
        from eth_utils import keccak

        evt_topic = "0x" + keccak(b"MemoryAnchored(address,bytes32,uint256)").hex()
        logs = receipt.get("logs", []) or []
        out["event_emitted"] = any(
            (lg.get("topics") and lg["topics"][0] == evt_topic) for lg in logs
        )
    return out


def _cli() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--rpc-url", required=True)
    p.add_argument("--pk", required=True)
    p.add_argument("--anchor", required=True, help="MemoryAnchor contract address")
    p.add_argument("--root", default=None, help="32-byte hex root; default: compute from --memory")
    p.add_argument(
        "--memory",
        default="/tmp/alice.mem",
        help="Path to Alice's MemoryService savefile (used when --root absent)",
    )
    p.add_argument("--no-wait", action="store_true", help="Skip receipt wait")
    args = p.parse_args()

    if args.root:
        root_hex = args.root
    else:
        mem = MemoryService.load(args.memory)
        root_hex = "0x" + mem.pinned_merkle_root().hex()

    result = anchor_memory(
        rpc_url=args.rpc_url,
        pk=args.pk,
        anchor_address=args.anchor,
        root_hex=root_hex,
        wait=not args.no_wait,
    )
    print(json.dumps(result))


if __name__ == "__main__":
    _cli()
