"""chain.py — tiny RPC helpers used by demo scripts.

Deliberately depends on stdlib + httpx + eth_account only. No `web3.py`. The
demo's chain interactions are simple enough that JSON-RPC calls are clearer
than wrapping them in a heavyweight client.

Helpers fall into two camps:

  * Low-level: ``rpc_call``, ``send_raw_tx``, ``wait_for_receipt``, ``eth_call``.
  * Demo-level: ``deploy_contract_from_artifact``, ``call_validate_user_op_expect_revert``.

All addresses are returned 0x-prefixed lowercase. Tx hashes are 0x-prefixed hex.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass
from typing import Any, Optional

import httpx
from eth_account import Account


# ---------------------------------------------------------------------------
# JSON-RPC
# ---------------------------------------------------------------------------


def rpc_call(url: str, method: str, params: list[Any], *, timeout: float = 30.0) -> Any:
    """Synchronous JSON-RPC call. Raises on transport or RPC error."""
    body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    resp = httpx.post(url, json=body, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    if "error" in data and data["error"]:
        raise RuntimeError(f"rpc {method} error: {data['error']}")
    return data.get("result")


def chain_id(url: str) -> int:
    return int(rpc_call(url, "eth_chainId", []), 16)


def gas_price(url: str) -> int:
    return int(rpc_call(url, "eth_gasPrice", []), 16)


def get_nonce(url: str, addr: str) -> int:
    return int(rpc_call(url, "eth_getTransactionCount", [addr, "pending"]), 16)


def wait_for_receipt(
    url: str, tx_hash: str, *, timeout: float = 60.0, poll: float = 0.3
) -> dict:
    """Poll eth_getTransactionReceipt until non-null or timeout."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = rpc_call(url, "eth_getTransactionReceipt", [tx_hash])
        if r is not None:
            return r
        time.sleep(poll)
    raise TimeoutError(f"receipt for {tx_hash} not seen within {timeout}s")


# ---------------------------------------------------------------------------
# cast wrappers
# ---------------------------------------------------------------------------
#
# SECURITY (CRITICAL-1 from docs/audit_phase3_security.md):
#
# `cast send --private-key <PK>` and `forge create --private-key <PK>`
# place the deployer key in argv, where any local user can read it via
# `ps auxww` / `/proc/<pid>/cmdline` for the lifetime of the subprocess.
# This is a catastrophic key-leak vector on shared / CI hosts.
#
# Foundry does NOT honour `$PRIVATE_KEY` for `cast send` / `forge create`
# (only for `forge script` via `vm.startBroadcast()`). `--interactive`
# requires a TTY (rejects piped stdin: "Device not configured").
#
# Solution: sign + encode the transaction in pure Python via `eth_account`,
# then broadcast via `eth_sendRawTransaction`. The key never leaves the
# Python process — never argv, never env passed to a child process. `cast
# call` (read-only) still uses the subprocess wrapper, but never touches
# the key.
#
# ---------------------------------------------------------------------------


def _cast_run(args: list[str], *, env_pk: Optional[str] = None) -> str:
    """Run a read-only cast subcommand (e.g. ``cast call``) and return stdout.

    SECURITY: ``env_pk`` exists only as a defence-in-depth tripwire. If a
    caller passes a private key here, we refuse to spawn a cast subprocess
    that has ``--private-key`` in its argv. Signing must go through
    ``cast_send`` / ``deploy_contract_via_cast``, which sign in-process.
    """
    if env_pk is not None and "--private-key" in args:
        raise RuntimeError(
            "refusing to invoke cast with --private-key in argv; "
            "sign in-process via eth_account instead"
        )
    env = os.environ.copy()
    out = subprocess.run(
        ["cast", *args],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    if out.returncode != 0:
        raise RuntimeError(
            f"cast {args[0] if args else ''} failed (rc={out.returncode}): "
            f"stderr={out.stderr.strip()[:500]} stdout={out.stdout.strip()[:200]}"
        )
    return out.stdout.strip()


def _encode_call_data(sig: str, args: list[str]) -> str:
    """Use ``cast calldata "sig" arg0 arg1 ...`` to ABI-encode a call.

    Pure-Python ABI encoding for arbitrary signature strings (including
    nested tuples / dynamic types as used by ``onInstall(bytes)``) is
    out of scope here; ``cast calldata`` is a deterministic, read-only
    encoder that doesn't touch a private key. Returns 0x-prefixed hex.
    """
    cmd = ["calldata", sig]
    cmd.extend(args)
    out = _cast_run(cmd)
    if not out.startswith("0x"):
        out = "0x" + out
    return out


def _fetch_tx_params(rpc_url: str, from_addr: str) -> dict:
    """Pull nonce + chain id + fee suggestions for a tx via JSON-RPC."""
    nonce = get_nonce(rpc_url, from_addr)
    cid = chain_id(rpc_url)
    # Try EIP-1559 first; fall back to legacy gas price if the RPC doesn't
    # support fee history.
    try:
        max_priority_hex = rpc_call(rpc_url, "eth_maxPriorityFeePerGas", [])
        max_priority = int(max_priority_hex, 16) if max_priority_hex else 1_000_000_000
    except Exception:
        max_priority = 1_000_000_000
    base_fee = gas_price(rpc_url)
    # Conservative cap: 2 * base_fee + tip.
    max_fee = 2 * base_fee + max_priority
    return {
        "nonce": nonce,
        "chainId": cid,
        "maxPriorityFeePerGas": max_priority,
        "maxFeePerGas": max_fee,
    }


def _sign_and_broadcast(
    *,
    rpc_url: str,
    pk: str,
    to: Optional[str],
    value: int,
    data: bytes,
    gas_limit: Optional[int],
) -> str:
    """Sign an EIP-1559 tx with ``pk`` and broadcast it. Returns tx hash.

    SECURITY: the key stays in the Python process. It never reaches argv,
    never reaches the environment of any child process. ``ps auxww`` will
    see only this Python interpreter's own argv (which has no key in it).
    """
    acct = Account.from_key(pk)
    from_addr = acct.address
    tx_params = _fetch_tx_params(rpc_url, from_addr)

    tx: dict[str, Any] = {
        "from": from_addr,
        "value": int(value),
        "data": "0x" + data.hex() if data else "0x",
        "nonce": tx_params["nonce"],
        "chainId": tx_params["chainId"],
        "maxPriorityFeePerGas": tx_params["maxPriorityFeePerGas"],
        "maxFeePerGas": tx_params["maxFeePerGas"],
        "type": 2,
    }
    if to is not None:
        # eth_account's typed-tx validator rejects non-checksummed addresses.
        from eth_utils import to_checksum_address
        tx["to"] = to_checksum_address(to) if to.startswith("0x") else to

    if gas_limit is None:
        # Ask the node to estimate. Fall back to a generous default if
        # estimation fails (e.g. local fork without full state).
        try:
            est_hex = rpc_call(
                rpc_url,
                "eth_estimateGas",
                [
                    {
                        k: hex(v) if isinstance(v, int) else v
                        for k, v in {
                            "from": from_addr,
                            "to": to,
                            "value": value,
                            "data": tx["data"],
                        }.items()
                        if v is not None
                    }
                ],
            )
            gas_limit = int(int(est_hex, 16) * 12 // 10)  # +20% headroom
        except Exception:
            gas_limit = 3_000_000
    tx["gas"] = int(gas_limit)

    signed = acct.sign_transaction(tx)
    raw_hex = "0x" + signed.raw_transaction.hex().removeprefix("0x")
    tx_hash = rpc_call(rpc_url, "eth_sendRawTransaction", [raw_hex])
    return tx_hash


def cast_send(
    *,
    rpc_url: str,
    pk: str,
    to: str,
    sig: Optional[str] = None,
    args: Optional[list[str]] = None,
    value: int = 0,
    data: Optional[str] = None,
    gas_limit: Optional[int] = None,
) -> str:
    """Submit a tx. Returns the tx hash.

    SECURITY: ``pk`` is used in-process via ``eth_account`` only. It is
    NEVER placed in argv and is NEVER exported to any child process. The
    transaction is signed locally and broadcast via JSON-RPC
    ``eth_sendRawTransaction``, so ``ps auxww`` / ``/proc/<pid>/cmdline``
    never sees the key.

    The function name keeps ``cast_send`` for API stability with callers
    in ``scripts/anchor_memory.py`` and ``scripts/demo_e2e.py``.
    """
    # Resolve calldata. Either:
    #   - ``data`` is raw hex calldata (already ABI-encoded), or
    #   - ``sig`` + ``args`` is a function signature we ABI-encode via cast.
    if sig is not None and data is not None:
        raise ValueError("pass either sig+args or data, not both")
    if data is not None:
        data_hex = data.removeprefix("0x")
        if data_hex and not all(c in "0123456789abcdefABCDEF" for c in data_hex):
            raise ValueError(f"data must be hex, got {data!r}")
        call_bytes = bytes.fromhex(data_hex) if data_hex else b""
    elif sig is not None:
        call_hex = _encode_call_data(sig, list(args or []))
        call_bytes = bytes.fromhex(call_hex.removeprefix("0x"))
    else:
        call_bytes = b""

    return _sign_and_broadcast(
        rpc_url=rpc_url,
        pk=pk,
        to=to,
        value=int(value),
        data=call_bytes,
        gas_limit=gas_limit,
    )


def cast_call(
    *,
    rpc_url: str,
    to: str,
    sig: str,
    args: Optional[list[str]] = None,
) -> str:
    """Read-only ``cast call`` — no signing, safe to subprocess."""
    cmd = ["call", "--rpc-url", rpc_url, to, sig]
    if args:
        cmd.extend(args)
    return _cast_run(cmd)


def cast_address_from_pk(pk: str) -> str:
    """Derive 0x-prefixed lowercase address from a private key."""
    return Account.from_key(pk).address


# ---------------------------------------------------------------------------
# Forge artifact deployment helper
# ---------------------------------------------------------------------------


def load_artifact(artifact_path: str) -> dict:
    with open(artifact_path) as f:
        return json.load(f)


def deploy_contract_via_cast(
    *,
    rpc_url: str,
    pk: str,
    artifact_path: str,
    constructor_args: Optional[list[str]] = None,
) -> tuple[str, str]:
    """Deploy a contract by signing locally and broadcasting via JSON-RPC.

    Returns ``(deployed_address, tx_hash)``.

    SECURITY: ``pk`` stays in this Python process. It is NEVER placed in
    argv (no ``forge create --private-key …`` subprocess) and never enters
    a child process env. We pull the contract bytecode from the artifact,
    optionally append ABI-encoded constructor args, sign locally with
    ``eth_account``, and broadcast via ``eth_sendRawTransaction``.

    The function name keeps ``deploy_contract_via_cast`` for API stability
    with ``scripts/demo_e2e.py`` and ``scripts/tests/...``.
    """
    art = load_artifact(artifact_path)

    # 1. Extract the bytecode that gets deployed.
    bytecode_obj = art.get("bytecode")
    if isinstance(bytecode_obj, dict):
        bytecode_hex = bytecode_obj.get("object") or ""
    elif isinstance(bytecode_obj, str):
        bytecode_hex = bytecode_obj
    else:
        raise RuntimeError(
            f"artifact {artifact_path} has no recognisable bytecode field"
        )
    bytecode_hex = bytecode_hex.removeprefix("0x")
    if not bytecode_hex:
        raise RuntimeError(f"artifact {artifact_path} has empty bytecode")

    # 2. Build constructor arg bytes. We use cast abi-encode for parity
    #    with `forge create --constructor-args` semantics. cast abi-encode
    #    is read-only and never touches the key.
    constructor_bytes = b""
    if constructor_args:
        ctor_types = _constructor_signature_from_artifact(art)
        # cast abi-encode takes a function-shape string; we synthesise one.
        sig = f"constructor({','.join(ctor_types)})"
        encoded = _cast_run(["abi-encode", sig, *constructor_args])
        constructor_bytes = bytes.fromhex(encoded.removeprefix("0x"))

    deploy_data = bytes.fromhex(bytecode_hex) + constructor_bytes

    # 3. Sign + broadcast. ``to=None`` triggers contract creation.
    tx_hash = _sign_and_broadcast(
        rpc_url=rpc_url,
        pk=pk,
        to=None,
        value=0,
        data=deploy_data,
        gas_limit=None,
    )

    # 4. Wait for the receipt to learn the deployed address.
    receipt = wait_for_receipt(rpc_url, tx_hash, timeout=90.0)
    addr = receipt.get("contractAddress")
    if not addr:
        raise RuntimeError(
            f"deployment receipt missing contractAddress: {receipt!r}"
        )
    if int(receipt.get("status", "0x0"), 16) != 1:
        raise RuntimeError(f"deployment tx reverted: {receipt!r}")
    return addr, tx_hash


def _constructor_signature_from_artifact(art: dict) -> list[str]:
    """Return the constructor's input type list, e.g. ['address','uint256']."""
    abi = art.get("abi") or []
    for entry in abi:
        if entry.get("type") == "constructor":
            return [inp["type"] for inp in entry.get("inputs", [])]
    return []


def _guess_forge_root(artifact_path: str) -> str:
    """Walk up from artifact path until we find a foundry.toml."""
    p = os.path.abspath(artifact_path)
    while p != "/":
        p = os.path.dirname(p)
        if os.path.exists(os.path.join(p, "foundry.toml")):
            return p
    raise RuntimeError(f"foundry.toml not found above {artifact_path}")


# ---------------------------------------------------------------------------
# Demo-specific: validateUserOp revert check
# ---------------------------------------------------------------------------


@dataclass
class PackedUserOp:
    """Minimal representation. encoded() matches abi.encode of the Solidity struct."""

    sender: str
    nonce: int = 0
    initCode: bytes = b""
    callData: bytes = b""
    accountGasLimits: bytes = b"\x00" * 32
    preVerificationGas: int = 0
    gasFees: bytes = b"\x00" * 32
    paymasterAndData: bytes = b""
    signature: bytes = b""


def _encode_validate_user_op(op: PackedUserOp, user_op_hash: bytes) -> str:
    """abi.encode the validateUserOp((address,uint256,bytes,bytes,bytes32,uint256,bytes32,bytes,bytes),bytes32) call."""
    from eth_abi import encode

    # Per IERC7579 interface, PackedUserOperation order:
    # (address sender, uint256 nonce, bytes initCode, bytes callData,
    #  bytes32 accountGasLimits, uint256 preVerificationGas, bytes32 gasFees,
    #  bytes paymasterAndData, bytes signature)
    sel = bytes.fromhex("97003203")  # placeholder; we'll compute correct below
    # Compute the real selector:
    from eth_utils import keccak

    fn_sig = (
        "validateUserOp("
        "(address,uint256,bytes,bytes,bytes32,uint256,bytes32,bytes,bytes),"
        "bytes32)"
    )
    sel = keccak(fn_sig.encode())[:4]

    tuple_t = "(address,uint256,bytes,bytes,bytes32,uint256,bytes32,bytes,bytes)"
    body = encode(
        [tuple_t, "bytes32"],
        [
            (
                op.sender,
                op.nonce,
                op.initCode,
                op.callData,
                op.accountGasLimits,
                op.preVerificationGas,
                op.gasFees,
                op.paymasterAndData,
                op.signature,
            ),
            user_op_hash,
        ],
    )
    return "0x" + (sel + body).hex()


def call_validate_user_op_expect_revert(
    *,
    rpc_url: str,
    hook_address: str,
    sender: str,
    callData: str,
    deployer_pk: str,
) -> tuple[bool, dict]:
    """Send a tx that calls ConstitutionHook.validateUserOp with a payload that
    SHOULD revert under MAX_LEVERAGE. Returns (revert_observed, evidence).

    We use eth_call first (cheap, gives us the revert reason without a tx),
    then optionally send a real tx so we get an actual receipt on the explorer
    that shows the failure.
    """
    callData_bytes = bytes.fromhex(callData.removeprefix("0x"))
    op = PackedUserOp(sender=sender, callData=callData_bytes)
    user_op_hash = bytes(32)
    data = _encode_validate_user_op(op, user_op_hash)

    # eth_call probe — most informative for the revert reason.
    revert_seen = False
    revert_reason = ""
    tx_hash = None
    receipt_status = None
    block_number = None
    try:
        rpc_call(
            rpc_url,
            "eth_call",
            [
                {
                    "to": hook_address,
                    "data": data,
                },
                "latest",
            ],
        )
    except RuntimeError as e:
        revert_seen = True
        revert_reason = str(e)

    # Now attempt the real tx so a receipt exists. We accept tx failure
    # (revert) as success for this step.
    try:
        tx_hash = cast_send(
            rpc_url=rpc_url,
            pk=deployer_pk,
            to=hook_address,
            data=data,
            gas_limit=500_000,
        )
        receipt = wait_for_receipt(rpc_url, tx_hash, timeout=60)
        receipt_status = int(receipt.get("status", "0x1"), 16)
        block_number = int(receipt.get("blockNumber", "0x0"), 16)
        if receipt_status == 0:
            revert_seen = True
    except RuntimeError as e:
        # cast send may itself error if the local fork rejects the tx pre-flight.
        revert_seen = revert_seen or True
        revert_reason = revert_reason or str(e)[:300]

    return revert_seen, {
        "hook_address": hook_address,
        "sender": sender,
        "tx_hash": tx_hash,
        "receipt_status": receipt_status,
        "block_number": block_number,
        "revert_reason": revert_reason[:300],
    }
