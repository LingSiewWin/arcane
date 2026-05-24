"""resolve_bond.py — REAL Pyth-driven bond resolution against Arc testnet.

This is the off-chain half of the PerformanceOracle. It:

  1. Fetches a REAL price-update VAA from Pyth Hermes (free, no API key):
       GET https://hermes.pyth.network/v2/updates/price/latest?ids[]=<feed>
     The response's ``binary.data[0]`` is the hex-encoded Wormhole VAA that
     ``IPyth.updatePriceFeeds`` consumes; ``parsed[0].price`` is the live
     human-readable price (used only for logging / sanity, not trusted on-chain).

  2. Reads the on-chain ``getUpdateFee([vaa])`` from the real Arc Pyth so the
     resolve tx pays exactly the right fee (excess is refunded by the oracle).

  3. Submits ``PerformanceOracle.resolve(agent, [vaa])`` with that fee as
     ``value`` via the in-process signer in ``scripts/lib/chain.py`` (keystore
     or DEPLOYER_PK — the key never leaves the Python process).

  4. Waits for the receipt and parses the ``AdviceResolved`` event so the caller
     learns p0, p1, the realized return in bps, and whether a slash fired.

There is NO simulation mode. ``fetch_hermes_vaa`` is free and runs without a
key (proving the off-chain half is real); ``resolve_bond`` requires a funded
signer and hits real Arc Pyth + the real PerformanceOracle.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Optional

import httpx
from eth_abi import decode as abi_decode
from eth_utils import keccak

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.lib.chain import (  # noqa: E402
    cast_call,
    cast_send,
    rpc_call,
    wait_for_receipt,
)

# Canonical Pyth pull-oracle on Arc testnet (verified on-chain).
ARC_PYTH_DEFAULT = "0x2880aB155794e7179c9eE2e38200202908C17B43"
# SOL/USD Pyth feed id.
SOL_USD_FEED = "0xef0d8b6fda2ceba41da15d4095d1da392a0d2f8ed0c6c7bc0f4cfac8c280b56d"
HERMES_LATEST = "https://hermes.pyth.network/v2/updates/price/latest"

# AdviceResolved(address indexed agent, int64 p0, int64 p1, int256 rBps, bool slashed)
_ADVICE_RESOLVED_TOPIC = "0x" + keccak(
    b"AdviceResolved(address,int64,int64,int256,bool)"
).hex()


# ---------------------------------------------------------------------------
# Hermes (free, no key) — the REAL off-chain price source.
# ---------------------------------------------------------------------------


def fetch_hermes_vaa(
    feed_id: str = SOL_USD_FEED, *, timeout: float = 20.0
) -> dict[str, Any]:
    """Fetch the latest REAL price update for ``feed_id`` from Pyth Hermes.

    Returns a dict with:
      * ``vaa``        : 0x-prefixed hex VAA bytes for ``updatePriceFeeds``.
      * ``price``      : live integer price (Pyth fixed-point).
      * ``expo``       : price exponent.
      * ``conf``       : confidence interval (same scale).
      * ``publish_time``: unix seconds.
      * ``price_float``: human-readable ``price * 10**expo``.

    This is FREE and needs no API key — it proves the off-chain integration is
    real, not mocked.
    """
    feed = feed_id.removeprefix("0x")
    resp = httpx.get(
        HERMES_LATEST,
        params={"ids[]": feed, "encoding": "hex", "parsed": "true"},
        timeout=timeout,
    )
    resp.raise_for_status()
    data = resp.json()

    binary = data.get("binary") or {}
    blobs = binary.get("data") or []
    if not blobs:
        raise RuntimeError(f"Hermes returned no binary VAA for feed {feed_id}")
    vaa_hex = blobs[0]
    if not vaa_hex.startswith("0x"):
        vaa_hex = "0x" + vaa_hex

    parsed = data.get("parsed") or []
    if not parsed:
        raise RuntimeError(f"Hermes returned no parsed price for feed {feed_id}")
    price_obj = parsed[0]["price"]
    price = int(price_obj["price"])
    expo = int(price_obj["expo"])
    conf = int(price_obj["conf"])
    publish_time = int(price_obj["publish_time"])

    return {
        "vaa": vaa_hex,
        "price": price,
        "expo": expo,
        "conf": conf,
        "publish_time": publish_time,
        "price_float": price * (10 ** expo),
        "feed_id": feed_id,
    }


# ---------------------------------------------------------------------------
# On-chain: real Arc Pyth fee + real PerformanceOracle.resolve
# ---------------------------------------------------------------------------


def _get_update_fee(rpc_url: str, pyth_addr: str, vaa_hex: str) -> int:
    """Read ``getUpdateFee([vaa])`` from the real Arc Pyth contract."""
    out = cast_call(
        rpc_url=rpc_url,
        to=pyth_addr,
        sig="getUpdateFee(bytes[])(uint256)",
        args=["[" + vaa_hex + "]"],
    )
    # cast prints e.g. "1" or "1 [1e0]"; take the leading integer.
    return int(out.split()[0])


def _encode_resolve_calldata(agent: str, vaa_hex: str) -> str:
    """ABI-encode PerformanceOracle.resolve(address, bytes[])."""
    from eth_abi import encode as abi_encode
    from eth_utils import to_canonical_address

    sel = keccak(b"resolve(address,bytes[])")[:4]
    vaa_bytes = bytes.fromhex(vaa_hex.removeprefix("0x"))
    body = abi_encode(
        ["address", "bytes[]"],
        [to_canonical_address(agent), [vaa_bytes]],
    )
    return "0x" + (sel + body).hex()


def _parse_advice_resolved(receipt: dict, oracle_addr: str) -> Optional[dict]:
    """Decode the AdviceResolved event from a resolve() receipt, if present."""
    logs = receipt.get("logs", []) or []
    for lg in logs:
        topics = lg.get("topics") or []
        if not topics or topics[0].lower() != _ADVICE_RESOLVED_TOPIC.lower():
            continue
        if lg.get("address", "").lower() != oracle_addr.lower():
            continue
        agent_topic = topics[1] if len(topics) > 1 else None
        agent = "0x" + agent_topic[-40:] if agent_topic else None
        data_hex = lg.get("data", "0x").removeprefix("0x")
        p0, p1, r_bps, slashed = abi_decode(
            ["int64", "int64", "int256", "bool"], bytes.fromhex(data_hex)
        )
        return {
            "agent": agent,
            "p0": int(p0),
            "p1": int(p1),
            "r_bps": int(r_bps),
            "slashed": bool(slashed),
        }
    return None


def resolve_bond(
    *,
    rpc_url: str,
    pk: str,
    oracle_addr: str,
    agent: str,
    pyth_addr: str = ARC_PYTH_DEFAULT,
    feed_id: str = SOL_USD_FEED,
    fee_buffer: int = 0,
    gas_limit: int = 1_500_000,
) -> dict[str, Any]:
    """Run the REAL on-chain resolution against Arc.

    Fetches a live Hermes VAA, reads the real Arc Pyth update fee, submits
    ``PerformanceOracle.resolve(agent, [vaa])`` paying that fee, and parses the
    ``AdviceResolved`` event from the receipt.

    Returns a dict with the Hermes price, tx hash, receipt status, and the
    decoded verdict (``p0``, ``p1``, ``r_bps``, ``slashed``).
    """
    hermes = fetch_hermes_vaa(feed_id)
    vaa_hex = hermes["vaa"]

    fee = _get_update_fee(rpc_url, pyth_addr, vaa_hex)
    value = fee + max(0, int(fee_buffer))

    calldata = _encode_resolve_calldata(agent, vaa_hex)
    tx_hash = cast_send(
        rpc_url=rpc_url,
        pk=pk,
        to=oracle_addr,
        data=calldata,
        value=value,
        gas_limit=gas_limit,
    )
    receipt = wait_for_receipt(rpc_url, tx_hash, timeout=90.0)
    status = int(receipt.get("status", "0x0"), 16)
    if status != 1:
        raise RuntimeError(
            f"resolve() reverted (tx {tx_hash}, status {receipt.get('status')})"
        )

    event = _parse_advice_resolved(receipt, oracle_addr)
    return {
        "tx_hash": tx_hash,
        "status": status,
        "block_number": int(receipt.get("blockNumber", "0x0"), 16),
        "update_fee": fee,
        "value_sent": value,
        "hermes_price": hermes["price"],
        "hermes_price_float": hermes["price_float"],
        "hermes_expo": hermes["expo"],
        "hermes_conf": hermes["conf"],
        "hermes_publish_time": hermes["publish_time"],
        "advice_resolved": event,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _cli() -> int:
    p = argparse.ArgumentParser(
        description=(
            "Resolve a bonded advice via the real PerformanceOracle on Arc, "
            "using a real Pyth Hermes VAA. With --check-only, just fetch and "
            "print the live Hermes price (free, no key)."
        )
    )
    p.add_argument(
        "--check-only",
        action="store_true",
        help="Fetch + print the live Hermes price/VAA only. No tx, no key.",
    )
    p.add_argument("--feed-id", default=SOL_USD_FEED)
    p.add_argument("--rpc-url", default=None, help="Arc RPC (or $RPC).")
    p.add_argument("--oracle", default=None, help="PerformanceOracle address.")
    p.add_argument("--agent", default=None, help="Agent whose advice to resolve.")
    p.add_argument("--pyth", default=ARC_PYTH_DEFAULT, help="Arc Pyth address.")
    p.add_argument("--pk", default=None, help="DEPLOYER_PK (else --account/env).")
    p.add_argument(
        "--account",
        default=None,
        help="Foundry keystore name (decrypted in-process). Preferred over --pk.",
    )
    args = p.parse_args()

    if args.check_only:
        h = fetch_hermes_vaa(args.feed_id)
        print(json.dumps(
            {
                "feed_id": h["feed_id"],
                "price": h["price"],
                "expo": h["expo"],
                "conf": h["conf"],
                "publish_time": h["publish_time"],
                "price_float": h["price_float"],
                "vaa_bytes": len(h["vaa"]) // 2,
                "vaa_prefix": h["vaa"][:18] + "...",
            },
            indent=2,
        ))
        return 0

    import os

    rpc_url = args.rpc_url or os.environ.get("RPC", "")
    if not rpc_url:
        print("REFUSING: need --rpc-url or $RPC.", file=sys.stderr)
        return 3
    if not args.oracle or not args.agent:
        print("REFUSING: need --oracle and --agent for a live resolve.", file=sys.stderr)
        return 3

    if args.pk:
        pk = args.pk
    else:
        from scripts.lib.keys import KeyResolutionError, resolve_deployer_key

        try:
            pk = resolve_deployer_key(account=args.account, allow_interactive=True)
        except KeyResolutionError as e:
            print(f"REFUSING: {e}", file=sys.stderr)
            return 3
    if not pk:
        print("REFUSING: need --pk, --account, or DEPLOYER_PK.", file=sys.stderr)
        return 3

    result = resolve_bond(
        rpc_url=rpc_url,
        pk=pk,
        oracle_addr=args.oracle,
        agent=args.agent,
        pyth_addr=args.pyth,
        feed_id=args.feed_id,
    )
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
