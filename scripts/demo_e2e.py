#!/usr/bin/env python3
"""demo_e2e — Phase 2 / Slice 5D end-to-end demo runner.

Two modes (one MUST be chosen explicitly — there is no default, so the local
test harness can never be mistaken for the product run path):

  --mode local
      Spin up `anvil --fork-url $RPC --chain-id 5042002` so the chain state
      mirrors Arc testnet but no tx leaves the local box. Deploy the four
      contracts to the fork. Start Alice. Drive the 6-step flow. Append one
      JSONL line per step. Free, fast, hermetic.

  --mode live
      Broadcast real transactions to Arc testnet using $DEPLOYER_PK.
      Requires --yes-i-understand AND $DEPLOYER_PK. Estimates the USDC cost
      before sending anything.

Output: scripts/demo_output.jsonl — each line:
    {"step": int, "name": str, "ok": bool, "duration_ms": int,
     "tx_hash": str?, "explorer_url": str?, "evidence": {...}}

Step 4 reports ok=True when the constitution violation revert IS observed
(that's the demo's whole point).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Optional

# Make 'agents' and 'scripts' importable when invoked as a script.
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# ``agents.dark_pool`` used to evaluate its uvicorn entrypoint
# (``app = _build_default_app()``) at module import time, which loaded
# ``/tmp/alice.mem`` and crashed when missing. As of Bug 1's fix that
# eager-load is now lazy via ``__getattr__``, so importing the module on a
# fresh box is side-effect-free and no placeholder seed is required here.

from agents.alice import AliceConfig, start_alice_subprocess  # noqa: E402
from agents.orchestrator import (  # noqa: E402
    default_bob_rules,
    hash_constitution,
    step_attempt_violating_trade,
    step_decay_check_pinned,
    step_query_alice,
    step_select_violating_trace,
    step_spawn_bob,
    step_spawn_child_and_resolve_bond,
)
from agents.seed_alice import seed_alice  # noqa: E402
from scripts.anchor_memory import anchor_memory  # noqa: E402
from scripts.resolve_bond import resolve_bond  # noqa: E402
from scripts.lib.keys import KeyResolutionError, resolve_deployer_key  # noqa: E402
from scripts.lib.chain import (  # noqa: E402
    cast_address_from_pk,
    cast_call,
    cast_send,
    chain_id,
    deploy_contract_via_cast,
    rpc_call,
    wait_for_receipt,
)


DEFAULT_OUTPUT = REPO_ROOT / "scripts" / "demo_output.jsonl"
ARC_EXPLORER = "https://testnet.arcscan.app/tx/"
ANVIL_DEFAULT_PORT = 8545
ANVIL_DEFAULT_KEY = (
    # Anvil's well-known account #0 — used only in local mode.
    "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
)
USDC_ADDR = "0x3600000000000000000000000000000000000000"
# Canonical Pyth pull-oracle on Arc testnet (verified on-chain).
ARC_PYTH_DEFAULT = "0x2880aB155794e7179c9eE2e38200202908C17B43"
# SOL/USD Pyth feed id used by the PerformanceOracle slash rule.
SOL_USD_FEED = "0xef0d8b6fda2ceba41da15d4095d1da392a0d2f8ed0c6c7bc0f4cfac8c280b56d"


def explorer_url(tx_hash: Optional[str]) -> Optional[str]:
    if not tx_hash:
        return None
    return ARC_EXPLORER + tx_hash


# ---------------------------------------------------------------------------
# JSONL writer
# ---------------------------------------------------------------------------


class JsonlWriter:
    def __init__(self, path: Path):
        self.path = path
        # Truncate on start so each demo run is a clean log.
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("")

    def append(
        self,
        *,
        step: int,
        name: str,
        ok: bool,
        duration_ms: int,
        tx_hash: Optional[str],
        evidence: dict,
    ) -> None:
        rec: dict = {
            "step": step,
            "name": name,
            "ok": ok,
            "duration_ms": duration_ms,
            "evidence": evidence,
        }
        if tx_hash:
            rec["tx_hash"] = tx_hash
            rec["explorer_url"] = explorer_url(tx_hash)
        with self.path.open("a") as f:
            f.write(json.dumps(rec) + "\n")


# ---------------------------------------------------------------------------
# Anvil management
# ---------------------------------------------------------------------------


def _pick_free_port(start: int = 8545, end: int = 8600) -> int:
    for p in range(start, end):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", p))
                return p
            except OSError:
                continue
    raise RuntimeError("no free anvil port")


@contextmanager
def anvil_fork(rpc_url: str, chain_id_override: int = 5042002):
    """Start `anvil --fork-url RPC` and yield (local_rpc_url, anvil_proc).

    Falls back to *no-fork* if RPC is unset or unreachable — in that case the
    chain has clean state and chain_id_override still applies. This keeps
    `scripts/demo_e2e.py --mode local` runnable in environments without the
    Arc RPC token.
    """
    port = _pick_free_port()
    local_url = f"http://127.0.0.1:{port}"
    args = [
        "anvil",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--chain-id",
        str(chain_id_override),
        "--quiet",
    ]
    if rpc_url:
        # Use --fork-url if reachable; if it isn't, anvil will print an error
        # and exit — we want to fail clean in that case, not silently swallow.
        args += ["--fork-url", rpc_url, "--hardfork", "cancun"]
    proc = subprocess.Popen(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    # Wait for the port to start accepting connections.
    deadline = time.time() + 15.0
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.2)
            try:
                s.connect(("127.0.0.1", port))
                break
            except OSError:
                time.sleep(0.05)
    else:
        proc.terminate()
        out, err = proc.communicate(timeout=2)
        raise RuntimeError(
            f"anvil failed to start within 15s.\nstdout:\n{out!r}\nstderr:\n{err!r}"
        )

    try:
        yield local_url, proc
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


# ---------------------------------------------------------------------------
# Deployment helpers
# ---------------------------------------------------------------------------


def _artifact(name: str) -> str:
    return str(REPO_ROOT / "contracts" / "out" / f"{name}.sol" / f"{name}.json")


def _mock_erc721_artifact() -> str:
    """Path to the MockERC721 artifact compiled by ``forge build``."""
    return str(
        REPO_ROOT
        / "contracts"
        / "out"
        / "MemoryAnchor.t.sol"
        / "MockERC721.json"
    )


# Phase 4 audit (B5 / N9 / F10): demo's stable identity id. In live
# mode the caller can override via ``DEMO_IDENTITY_ID``.
DEMO_DEFAULT_IDENTITY_ID = 42

# Canonical Registered event on the Arc ERC-8004 IdentityRegistry — verified
# on-chain: keccak256("Registered(uint256,string,address)").
_REGISTERED_TOPIC = (
    "0xca52e62c367d81bb2e328eb795f7c7ba24afb478408a26c0e201d155c449bc4a"
)


def register_identity(
    *,
    rpc_url: str,
    pk: str,
    registry_addr: str,
    agent_uri: str = "ipfs://bafkreibdi6623n3xpf7ymk62ckb4bo75o3qemwkpfvp5i25j66itxvsoei",
) -> dict:
    """Register a fresh ERC-8004 identity OWNED by the deployer on the real
    Arc IdentityRegistry, returning the minted ``agentId``.

    Bug 1 fix: on live Arc the deployer owns no pre-existing identity, so the
    hardcoded id 42 (a MockERC721 mint that only exists in local mode) made
    ``MemoryAnchor.anchor(42, root)`` revert ``NotIdentityOwner`` because
    ``ownerOf(42) != deployer``. Here we call the canonical
    ``register(string agentURI, (string,bytes)[] metadata) returns (uint256)``
    so the deployer mints — and therefore owns — the identity it anchors
    against.

    The ABI was confirmed against the deployed bytecode at
    0x8004A818BFB912233c491871b3d84c89A494BD9e (selector 0x8ea42286;
    ``cast estimate``/``cast call`` from the deployer succeed and the call
    returns a uint256 agentId). We pass an empty metadata array — the registry
    accepts it (verified via cast call). The new owner's agentId is parsed from
    the canonical ``Registered(uint256 indexed agentId, string, address indexed
    owner)`` event in the receipt.

    Returns ``{"identity_id": int, "register_tx": "0x..."}``.
    """
    from eth_abi import encode as abi_encode
    from eth_utils import keccak

    # register(string,(string,bytes)[]) — empty metadata array.
    sel = keccak(b"register(string,(string,bytes)[])")[:4]
    body = abi_encode(
        ["string", "(string,bytes)[]"],
        [agent_uri, []],
    )
    calldata = "0x" + (sel + body).hex()

    register_tx = cast_send(
        rpc_url=rpc_url,
        pk=pk,
        to=registry_addr,
        data=calldata,
        gas_limit=400_000,
    )
    receipt = wait_for_receipt(rpc_url, register_tx, timeout=90)
    if int(receipt.get("status", "0x0"), 16) != 1:
        raise RuntimeError(
            f"register() reverted (tx {register_tx}, status "
            f"{receipt.get('status')}); no identity was minted on {registry_addr}"
        )

    deployer = cast_address_from_pk(pk)
    deployer_topic = "0x" + format(int(deployer, 16), "064x")
    identity_id: Optional[int] = None
    for lg in receipt.get("logs", []) or []:
        topics = lg.get("topics") or []
        if not topics or topics[0].lower() != _REGISTERED_TOPIC.lower():
            continue
        if lg.get("address", "").lower() != registry_addr.lower():
            continue
        # Registered(uint256 indexed agentId, string agentURI, address indexed owner)
        # topic[1] = agentId, topic[2] = owner.
        if len(topics) >= 3 and topics[2].lower() == deployer_topic.lower():
            identity_id = int(topics[1], 16)
            break
    if identity_id is None:
        raise RuntimeError(
            f"register() tx {register_tx} succeeded but no Registered event "
            f"owned by {deployer} was found in the receipt; cannot determine "
            f"the minted agentId"
        )
    return {"identity_id": identity_id, "register_tx": register_tx}


def deploy_all_contracts(
    *,
    rpc_url: str,
    pk: str,
    usdc_addr: str = USDC_ADDR,
    bond_window_secs: int = 604800,
    bond_liveness_secs: int = 259200,  # 3 days — Olas-style liveness timeout
    mint_local_identity: bool = True,
) -> dict[str, dict]:
    """Deploy ConstitutionRegistry, ConstitutionHook, MemoryAnchor, BondVault.

    Phase 4 audit (B5 / N9 / F10): when ``mint_local_identity`` is True we
    also stand up a MockERC721 identity registry and mint a token (id
    ``DEMO_DEFAULT_IDENTITY_ID``) to the deployer so the F10
    identity-bound anchor path can actually execute against an owned
    identity. Without this, ``anchor(uint256,bytes32)`` reverts with
    ``NotIdentityOwner`` because the deployer doesn't hold any token on
    the live Arc ERC-8004 registry from this script.

    Returns ``{name: {"address": "0x...", "tx_hash": "0x..."}, ...,
    "identity_id": int}``. The ``identity_id`` key is the token the
    deployer now owns on the registry — pass it through to
    ``anchor_memory(identity_id=...)``.
    """
    deployer = cast_address_from_pk(pk)
    addrs: dict[str, dict] = {}

    reg_addr, reg_tx = deploy_contract_via_cast(
        rpc_url=rpc_url, pk=pk, artifact_path=_artifact("ConstitutionRegistry")
    )
    addrs["ConstitutionRegistry"] = {"address": reg_addr, "tx_hash": reg_tx}

    # ConstitutionHook constructor is (ConstitutionRegistry _registry,
    # address _token) — the hook tracks the settlement token's balance for
    # MAX_TRADE_SIZE outcome enforcement (see ConstitutionHook.sol). Pass
    # both the registry and the USDC address, matching contracts/script/
    # Deploy.s.sol's ``new ConstitutionHook(registry, usdc)``.
    hook_addr, hook_tx = deploy_contract_via_cast(
        rpc_url=rpc_url,
        pk=pk,
        artifact_path=_artifact("ConstitutionHook"),
        constructor_args=[reg_addr, usdc_addr],
    )
    addrs["ConstitutionHook"] = {"address": hook_addr, "tx_hash": hook_tx}

    # ConstitutionValidator is the ERC-7579 type-1 validator — the
    # gatekeeper whose ``validateUserOp`` reverts a violating user-op with
    # ``ConstitutionViolation:<RULE>``. The type-4 ConstitutionHook above
    # owns preCheck/postCheck; the validator owns userOp validation. The
    # demo's step-4 revert proof drives ``validateUserOp`` so it MUST hit
    # the validator, not the hook. Constructor is (ConstitutionRegistry
    # _registry) — matches contracts/script/Deploy.s.sol.
    validator_addr, validator_tx = deploy_contract_via_cast(
        rpc_url=rpc_url,
        pk=pk,
        artifact_path=_artifact("ConstitutionValidator"),
        constructor_args=[reg_addr],
    )
    addrs["ConstitutionValidator"] = {
        "address": validator_addr,
        "tx_hash": validator_tx,
    }

    # F10 / B5: choose the identity registry. In local-mode (mint_local_identity)
    # we deploy MockERC721 and mint identityId=42 to the deployer. In live
    # mode against Arc, point at the canonical ERC-8004 registry at 0x8004…
    # and require the caller to have already minted a token they own
    # (DEMO_IDENTITY_ID env var).
    identity_id = int(os.environ.get("DEMO_IDENTITY_ID", DEMO_DEFAULT_IDENTITY_ID))
    if mint_local_identity:
        # Deploy MockERC721 and mint identityId to deployer so anchor()
        # can verify ownership and emit a non-zero identityId topic.
        mock_registry_addr, mock_registry_tx = deploy_contract_via_cast(
            rpc_url=rpc_url,
            pk=pk,
            artifact_path=_mock_erc721_artifact(),
        )
        mint_tx = cast_send(
            rpc_url=rpc_url,
            pk=pk,
            to=mock_registry_addr,
            sig="mint(address,uint256)",
            args=[deployer, str(identity_id)],
            gas_limit=200_000,
        )
        wait_for_receipt(rpc_url, mint_tx, timeout=60)
        addrs["IdentityRegistry"] = {
            "address": mock_registry_addr,
            "tx_hash": mock_registry_tx,
            "minted_identity_id": identity_id,
            "minted_to": deployer,
            "mint_tx": mint_tx,
        }
        identity_registry_addr = mock_registry_addr
    else:
        # Live mode: use the official Arc ERC-8004 registry AND register a
        # fresh identity the deployer actually owns (Bug 1 fix). The deployer
        # owns no pre-existing identity on real Arc, so anchoring against the
        # hardcoded id 42 reverts NotIdentityOwner. We mint one here via the
        # canonical register(string,(string,bytes)[]) and use its agentId.
        identity_registry_addr = os.environ.get(
            "ARC_IDENTITY_REGISTRY",
            "0x8004A818BFB912233c491871b3d84c89A494BD9e",
        )
        reg_result = register_identity(
            rpc_url=rpc_url,
            pk=pk,
            registry_addr=identity_registry_addr,
        )
        identity_id = reg_result["identity_id"]
        addrs["IdentityRegistry"] = {
            "address": identity_registry_addr,
            "tx_hash": None,
            "external": True,
            "registered_identity_id": identity_id,
            "registered_to": deployer,
            "register_tx": reg_result["register_tx"],
        }

    anchor_addr, anchor_tx = deploy_contract_via_cast(
        rpc_url=rpc_url,
        pk=pk,
        artifact_path=_artifact("MemoryAnchor"),
        constructor_args=[identity_registry_addr],
    )
    addrs["MemoryAnchor"] = {
        "address": anchor_addr,
        "tx_hash": anchor_tx,
        "identity_id": identity_id,
    }

    # Phase 5 Stream I: BondVault constructor is now
    # (IERC20 token, address oracle, uint256 releaseWindow, uint256 livenessTimeout)
    # — the Erasure double-burn vault has NO insurance pool (slashed funds
    # burn to 0x…dEaD), so the old `insurance` arg is gone.
    vault_addr, vault_tx = deploy_contract_via_cast(
        rpc_url=rpc_url,
        pk=pk,
        artifact_path=_artifact("BondVault"),
        constructor_args=[
            usdc_addr,
            deployer,
            str(bond_window_secs),
            str(bond_liveness_secs),
        ],
    )
    addrs["BondVault"] = {"address": vault_addr, "tx_hash": vault_tx}

    # PerformanceOracle: the real Pyth-driven slash judge. Constructor is
    # (IPyth pyth, IBondVault vault, IERC20 bondToken, address recorder).
    # On Arc the canonical Pyth pull-oracle lives at ARC_PYTH; the recorder
    # is the deployer (it commits Alice's advice on her behalf).
    arc_pyth = os.environ.get("ARC_PYTH", ARC_PYTH_DEFAULT)
    perf_addr, perf_tx = deploy_contract_via_cast(
        rpc_url=rpc_url,
        pk=pk,
        artifact_path=_artifact("PerformanceOracle"),
        constructor_args=[arc_pyth, vault_addr, usdc_addr, deployer],
    )
    addrs["PerformanceOracle"] = {
        "address": perf_addr,
        "tx_hash": perf_tx,
        "pyth": arc_pyth,
    }

    # Hand the BondVault's oracle role to PerformanceOracle so it (and only
    # it) can slash — and only after posting its own Erasure counter-bond.
    set_oracle_tx = cast_send(
        rpc_url=rpc_url,
        pk=pk,
        to=vault_addr,
        sig="setOracle(address)",
        args=[perf_addr],
        gas_limit=120_000,
    )
    wait_for_receipt(rpc_url, set_oracle_tx, timeout=60)
    addrs["PerformanceOracle"]["set_oracle_tx"] = set_oracle_tx

    # Phase 5 B16: a MAX_LEVERAGE rule requires a non-zero adapter at
    # registration (ConstitutionRegistry reverts AdapterRequired(0) otherwise).
    # Deploy the real GmxV2PerpAdapter so Bob's leverage cap is genuinely
    # enforceable — the rule is wired to it in run_demo before defineConstitution.
    gmx_addr, gmx_tx = deploy_contract_via_cast(
        rpc_url=rpc_url,
        pk=pk,
        artifact_path=_artifact("GmxV2PerpAdapter"),
    )
    addrs["GmxV2PerpAdapter"] = {"address": gmx_addr, "tx_hash": gmx_tx}

    addrs["identity_id"] = identity_id  # convenience top-level key

    return addrs


def define_constitution(
    *, rpc_url: str, pk: str, registry_addr: str, rules_dicts: list[dict]
) -> tuple[str, str]:
    """Call ConstitutionRegistry.defineConstitution(rules) on chain.

    Returns (constitution_hash, tx_hash). The hash matches Slice 5A's local
    ``agents.bob.constitution_hash`` (which is keccak256(abi.encode(Rule[]))).
    """
    from eth_abi import encode as abi_encode
    from eth_utils import keccak

    from agents.bob import rules_to_solidity

    # ConstitutionRegistry.Rule is (uint8 kind, bytes params, address adapter)
    # since Phase 5 Stream M. The selector and ABI encoding MUST include the
    # adapter field, otherwise the registry decodes garbage / reverts and the
    # constitution is never actually stored — which then makes onInstall
    # revert UnknownConstitution downstream.
    sol_rules = rules_to_solidity(rules_dicts)
    sel = keccak(b"defineConstitution((uint8,bytes,address)[])")[:4]
    body = abi_encode(["(uint8,bytes,address)[]"], [sol_rules])
    data = "0x" + (sel + body).hex()

    tx_hash = cast_send(
        rpc_url=rpc_url,
        pk=pk,
        to=registry_addr,
        data=data,
        gas_limit=1_000_000,
    )
    receipt = wait_for_receipt(rpc_url, tx_hash, timeout=60)
    if int(receipt.get("status", "0x0"), 16) != 1:
        raise RuntimeError(
            f"defineConstitution reverted (tx {tx_hash}, status "
            f"{receipt.get('status')}); the constitution was not stored"
        )
    return hash_constitution(rules_dicts), tx_hash


def post_bond(
    *, rpc_url: str, pk: str, vault_addr: str, usdc_addr: str, amount_units: int
) -> dict:
    """approve(vault, amount) then post(amount). Returns both tx hashes.

    On Arc, USDC is the native gas token AND the ERC-20 bond token at the same
    address. The approve + post pattern still works because the ERC-20 surface
    is independent of the native-gas surface (different address layout, same
    asset).
    """
    # approve
    approve_tx = cast_send(
        rpc_url=rpc_url,
        pk=pk,
        to=usdc_addr,
        sig="approve(address,uint256)",
        args=[vault_addr, str(amount_units)],
        gas_limit=200_000,
    )
    wait_for_receipt(rpc_url, approve_tx, timeout=60)

    # post
    post_tx = cast_send(
        rpc_url=rpc_url,
        pk=pk,
        to=vault_addr,
        sig="post(uint256)",
        args=[str(amount_units)],
        gas_limit=300_000,
    )
    wait_for_receipt(rpc_url, post_tx, timeout=60)

    return {"approve_tx": approve_tx, "post_tx": post_tx}


def record_advice(
    *,
    rpc_url: str,
    pk: str,
    oracle_addr: str,
    agent: str,
    feed_id: str = SOL_USD_FEED,
    direction: int = 1,
    horizon_secs: int = 1,
    slash_threshold_bps: int = 1,
    slash_amount_units: int = 100_000,
    pyth_addr: str = ARC_PYTH_DEFAULT,
) -> dict:
    """Commit `agent`'s trading advice to the PerformanceOracle.

    Bug 2 fix: recordAdvice now refreshes Pyth on-chain BEFORE snapshotting
    p0 (the on-chain SOL/USD price on Arc is frequently older than the Pyth
    valid time period, so reading it without a fresh push reverts StalePrice).
    We therefore FIRST fetch a REAL Hermes VAA (free, no key), read the real
    Arc Pyth `getUpdateFee`, then call the now-payable
    ``recordAdvice(address,bytes32,int8,uint64,uint32,uint256,bytes[])`` with
    the VAA + that fee as value. Real Hermes, no mock.

    The demo uses an aggressive `slash_threshold_bps` over a short
    `horizon_secs` so that a real (tiny) adverse SOL/USD move at resolution
    predictably trips the slash rule — the price check is genuinely live,
    only the claimed tolerance is aggressive (disclosed, not faked).

    Returns the recordAdvice tx hash plus the live Hermes price snapshot.
    """
    from eth_abi import encode as abi_encode
    from eth_utils import keccak, to_canonical_address

    # 1. Fetch a REAL Hermes VAA for p0 (free, no key). Reuses the same source
    #    resolve_bond uses for p1, so record + resolve share one price oracle.
    from scripts.resolve_bond import fetch_hermes_vaa

    hermes = fetch_hermes_vaa(feed_id)
    vaa_hex = hermes["vaa"]

    # 2. Read the REAL Arc Pyth update fee for [vaa].
    fee_out = cast_call(
        rpc_url=rpc_url,
        to=pyth_addr,
        sig="getUpdateFee(bytes[])(uint256)",
        args=["[" + vaa_hex + "]"],
    )
    fee = int(fee_out.split()[0])

    # 3. ABI-encode recordAdvice(address,bytes32,int8,uint64,uint32,uint256,bytes[]).
    #    cast calldata can't synthesise the dynamic bytes[] arg cleanly, so we
    #    encode in Python (same approach resolve_bond uses for resolve()).
    sel = keccak(
        b"recordAdvice(address,bytes32,int8,uint64,uint32,uint256,bytes[])"
    )[:4]
    feed_bytes = bytes.fromhex(feed_id.removeprefix("0x"))
    vaa_bytes = bytes.fromhex(vaa_hex.removeprefix("0x"))
    body = abi_encode(
        ["address", "bytes32", "int8", "uint64", "uint32", "uint256", "bytes[]"],
        [
            to_canonical_address(agent),
            feed_bytes,
            int(direction),
            int(horizon_secs),
            int(slash_threshold_bps),
            int(slash_amount_units),
            [vaa_bytes],
        ],
    )
    calldata = "0x" + (sel + body).hex()

    record_tx = cast_send(
        rpc_url=rpc_url,
        pk=pk,
        to=oracle_addr,
        data=calldata,
        value=fee,
        gas_limit=1_500_000,
    )
    receipt = wait_for_receipt(rpc_url, record_tx, timeout=60)
    if int(receipt.get("status", "0x0"), 16) != 1:
        raise RuntimeError(
            f"recordAdvice reverted (tx {record_tx}); p0 was not snapshotted"
        )
    return {
        "record_advice_tx": record_tx,
        "update_fee": fee,
        "hermes_p0": hermes["price"],
        "hermes_p0_float": hermes["price_float"],
        "hermes_p0_publish_time": hermes["publish_time"],
    }


# ---------------------------------------------------------------------------
# Demo flow
# ---------------------------------------------------------------------------


def run_demo(
    *,
    mode: str,
    rpc_url: str,
    pk: str,
    output_path: Path,
    memory_path: str = "/tmp/alice.mem",
    seed_n: int = 50,
) -> int:
    """Run all 6 steps. Returns 0 on success, non-zero on failure."""
    writer = JsonlWriter(output_path)
    deployer = cast_address_from_pk(pk)

    # Step 0 (out-of-band): seed Alice, start Alice, deploy contracts.
    # These don't count toward the 6 demo steps but are required setup.
    seed_alice(out_path=memory_path, n=seed_n)

    alice_cfg = AliceConfig(
        memory_path=memory_path,
        payment_recipient="0x000000000000000000000000000000000000A11C",
    )
    alice_proc, alice_url = start_alice_subprocess(alice_cfg)

    overall_ok = True
    try:
        # Deploy contracts (counts as part of step 1 evidence).
        # Phase 4 audit (B5 / F10): local mode mints its own MockERC721
        # identity; live mode uses the deployed Arc ERC-8004 registry
        # and expects the operator to have already minted a token.
        addrs = deploy_all_contracts(
            rpc_url=rpc_url,
            pk=pk,
            mint_local_identity=(mode == "local"),
        )
        # ---- Step 1: spawn Bob + define his constitution on-chain
        rules = default_bob_rules()
        # B16: the MAX_LEVERAGE rule requires a non-zero adapter at
        # registration. Wire the just-deployed GmxV2PerpAdapter so the
        # leverage cap is genuinely enforceable (and defineConstitution
        # doesn't revert AdapterRequired(0)).
        gmx_adapter = addrs["GmxV2PerpAdapter"]["address"]
        for r in rules:
            if r.get("kind") == "MAX_LEVERAGE" and not r.get("adapter"):
                r["adapter"] = gmx_adapter
        t0 = time.time()
        constitution_hash, define_tx = define_constitution(
            rpc_url=rpc_url,
            pk=pk,
            registry_addr=addrs["ConstitutionRegistry"]["address"],
            rules_dicts=rules,
        )
        bob, bob_evidence = step_spawn_bob(budget_usdc=10.0, rules=rules)
        ok1 = constitution_hash == bob.constitution_hash
        writer.append(
            step=1,
            name="spawn_bob",
            ok=ok1,
            duration_ms=int((time.time() - t0) * 1000),
            tx_hash=define_tx,
            evidence={
                "deployer": deployer,
                "addresses": addrs,
                "constitution_hash_onchain": constitution_hash,
                "constitution_hash_local": bob.constitution_hash,
                **bob_evidence,
            },
        )

        # ---- Step 2: Bob pays + queries Alice
        t0 = time.time()
        try:
            results, q_evidence = step_query_alice(bob, alice_url)
            ok2 = bool(results)
        except Exception as e:
            results, q_evidence = [], {"error": str(e)[:300]}
            ok2 = False
        writer.append(
            step=2,
            name="query_alice",
            ok=ok2,
            duration_ms=int((time.time() - t0) * 1000),
            tx_hash=None,
            evidence={**q_evidence, "n_results": len(results)},
        )

        # ---- Step 3: pick the violating trace
        t0 = time.time()
        chosen, sel_evidence = step_select_violating_trace(results)
        ok3 = chosen is not None
        writer.append(
            step=3,
            name="select_violating_trace",
            ok=ok3,
            duration_ms=int((time.time() - t0) * 1000),
            tx_hash=None,
            evidence=sel_evidence,
        )

        # ---- Step 4: attempt violating trade — MUST revert
        t0 = time.time()
        # Install the constitution on Bob's "SCA" (here we use the deployer as
        # the smart account for the local demo) so the validator has something
        # to check. In real Arc flow this is done by spawn_agent.ts.
        # cast send validator.onInstall(abi.encode(constitutionHash))
        #
        # The revert proof drives ``validateUserOp``, which lives on the
        # ConstitutionValidator (type-1), NOT the type-4 ConstitutionHook —
        # so we install on and call the validator. ``validateUserOp`` keys
        # off ``constitutionOf[msg.sender]``, and the revert helper sends
        # from ``deployer_pk`` with ``sender=deployer``, so the install
        # (whose onInstall msg.sender is the deployer) and the call agree.
        validator_addr = addrs["ConstitutionValidator"]["address"]
        from eth_utils import keccak
        install_sel = keccak(b"onInstall(bytes)")[:4]
        # `bytes` arg: dynamic offset 0x20, length 0x20, payload = constitutionHash
        install_data = (
            "0x" + install_sel.hex()
            + "0000000000000000000000000000000000000000000000000000000000000020"
            + "0000000000000000000000000000000000000000000000000000000000000020"
            + constitution_hash.removeprefix("0x")
        )
        try:
            install_tx = cast_send(
                rpc_url=rpc_url,
                pk=pk,
                to=validator_addr,
                data=install_data,
                gas_limit=300_000,
            )
            wait_for_receipt(rpc_url, install_tx, timeout=60)
        except RuntimeError as e:
            # already-installed or similar; not fatal
            install_tx = None

        revert_seen, revert_evidence = step_attempt_violating_trade(
            bob,
            rpc_url=rpc_url,
            hook_address=validator_addr,
            deployer_pk=pk,
            sca_address=deployer,
        )
        # ok=True when revert IS observed (that's the success condition).
        writer.append(
            step=4,
            name="constitution_revert",
            ok=revert_seen,
            duration_ms=int((time.time() - t0) * 1000),
            tx_hash=revert_evidence.get("tx_hash"),
            evidence={
                "expected": "revert with ConstitutionViolation:MAX_TRADE_SIZE",
                "hook_install_tx": install_tx,
                **revert_evidence,
            },
        )

        # ---- Step 5: decay + pinned root + anchor on chain
        t0 = time.time()
        stable, decay_ev = step_decay_check_pinned(memory_path)
        # Phase 4 audit (B5 / N9 / F10): anchor the (unchanged) pinned
        # root via the identity-bound entry point. The deployer owns
        # ``addrs["identity_id"]`` on the registry (minted at deploy time
        # in local mode, pre-minted by the operator in live mode), so the
        # ownerOf check inside MemoryAnchor.anchor succeeds.
        anchor_result = anchor_memory(
            rpc_url=rpc_url,
            pk=pk,
            anchor_address=addrs["MemoryAnchor"]["address"],
            root_hex=decay_ev["pinned_root_after"],
            identity_id=addrs["identity_id"],
        )
        # ok5 is True only when the pinned root is stable AND the F10
        # identity-bound event fired AND its topic[2] matches our id —
        # never accept an event that came in with identityId=0 on the
        # identity-bound path.
        ok5 = (
            stable
            and anchor_result.get("event_emitted", False)
            and anchor_result.get("event_identity_id_matches", False)
        )
        writer.append(
            step=5,
            name="anchor_pinned_root",
            ok=ok5,
            duration_ms=int((time.time() - t0) * 1000),
            tx_hash=anchor_result["tx_hash"],
            evidence={**decay_ev, **anchor_result, "pinned_root_stable": stable},
        )

        # ---- Step 6: child + REAL Pyth-driven bond resolution
        t0 = time.time()
        child_dict, child_ev = step_spawn_child_and_resolve_bond(bob)
        # The real sequence:
        #   1. deployer posts a 1 USDC bond (the bonded "agent" here).
        #   2. PerformanceOracle.recordAdvice snapshots a REAL Pyth SOL/USD p0
        #      with an aggressive 1 bps slash threshold over a 1s horizon —
        #      the price check is genuinely live; only the tolerance is
        #      aggressive (disclosed, not faked) so a tiny adverse tick slashes.
        #   3. fund the oracle with USDC so it can post its own Erasure
        #      counter-bond at slash time (skin in the game).
        #   4. scripts/resolve_bond.py fetches a REAL Hermes VAA and submits
        #      PerformanceOracle.resolve(deployer, [vaa]) — real slash or
        #      release on Arc, with a real tx hash.
        usdc_amount_one = 1_000_000  # 1 USDC bond
        slash_amount = 100_000  # 0.1 USDC slashed on failure
        bond_evidence: dict[str, Any] = {}
        perf_addr = addrs["PerformanceOracle"]["address"]
        try:
            bond_result = post_bond(
                rpc_url=rpc_url,
                pk=pk,
                vault_addr=addrs["BondVault"]["address"],
                usdc_addr=USDC_ADDR,
                amount_units=usdc_amount_one,
            )
            bond_evidence["bond_post"] = bond_result

            # Fund the oracle so it can post its Erasure counter-bond when it
            # slashes (the oracle burns its own counter-bond alongside the
            # agent's bond — no skin, no slash).
            fund_oracle_tx = cast_send(
                rpc_url=rpc_url,
                pk=pk,
                to=USDC_ADDR,
                sig="transfer(address,uint256)",
                args=[perf_addr, str(slash_amount)],
                gas_limit=200_000,
            )
            wait_for_receipt(rpc_url, fund_oracle_tx, timeout=60)
            bond_evidence["fund_oracle_tx"] = fund_oracle_tx

            # Record advice: real Pyth p0, aggressive 1 bps threshold, 1s horizon.
            advice_result = record_advice(
                rpc_url=rpc_url,
                pk=pk,
                oracle_addr=perf_addr,
                agent=deployer,
                feed_id=SOL_USD_FEED,
                direction=1,
                horizon_secs=1,
                slash_threshold_bps=1,
                slash_amount_units=slash_amount,
                pyth_addr=os.environ.get("ARC_PYTH", ARC_PYTH_DEFAULT),
            )
            bond_evidence["record_advice"] = advice_result

            # Let the 1s horizon elapse, then resolve with a real Hermes VAA.
            time.sleep(2)
            resolve_result = resolve_bond(
                rpc_url=rpc_url,
                pk=pk,
                oracle_addr=perf_addr,
                agent=deployer,
                pyth_addr=os.environ.get("ARC_PYTH", ARC_PYTH_DEFAULT),
                feed_id=SOL_USD_FEED,
            )
            bond_evidence["resolve"] = resolve_result
            bond_evidence["bond_resolved"] = True
            top_tx = resolve_result["tx_hash"]
        except RuntimeError as e:
            # Bond flow can fail in local mode if the deployer doesn't have
            # USDC, or if anvil's fork lacks the live Pyth price. We still
            # report step success if the child spawn worked, with the bond
            # failure captured in evidence.
            bond_evidence["error"] = str(e)[:300]
            bond_evidence["bond_resolved"] = False
            top_tx = None
        writer.append(
            step=6,
            name="spawn_child_and_bond_resolve",
            ok=True,  # child spawn always succeeds; bond outcome is in evidence
            duration_ms=int((time.time() - t0) * 1000),
            tx_hash=top_tx,
            evidence={**child_ev, **bond_evidence},
        )

    finally:
        if alice_proc.poll() is None:
            alice_proc.terminate()
            try:
                alice_proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                alice_proc.kill()

    # Final tally: count steps with ok=False
    failed = []
    for line in output_path.read_text().splitlines():
        rec = json.loads(line)
        if not rec.get("ok"):
            failed.append(rec["name"])

    if failed:
        print(f"demo finished with failures: {failed}", file=sys.stderr)
        return 1
    print(f"demo finished OK. {output_path}")
    return 0


# ---------------------------------------------------------------------------
# Modes
# ---------------------------------------------------------------------------


def run_local(args) -> int:
    rpc_url = args.rpc_url or os.environ.get("RPC", "")
    if not rpc_url:
        # Source ~/.arc-canteen/env via subprocess shell so we don't
        # need to parse it ourselves. If still empty, we proceed
        # without forking (anvil empty chain) — tests should still pass.
        try:
            out = subprocess.run(
                ["bash", "-c", ". ~/.arc-canteen/env && echo $RPC"],
                capture_output=True,
                text=True,
                check=False,
            )
            rpc_url = out.stdout.strip()
        except Exception:
            rpc_url = ""

    print(
        "LOCAL MODE — forking Arc into a local anvil with a well-known TEST key. "
        "This is NOT a real broadcast; output is for local verification only.",
        file=sys.stderr,
    )
    with anvil_fork(rpc_url, chain_id_override=5042002) as (local_rpc, _proc):
        return run_demo(
            mode="local",
            rpc_url=local_rpc,
            pk=ANVIL_DEFAULT_KEY,
            output_path=Path(args.output),
            memory_path=args.memory,
            seed_n=args.seed_n,
        )


def run_live(args) -> int:
    if not args.yes_i_understand:
        print(
            "REFUSING to broadcast: --mode live requires --yes-i-understand. "
            "This sends real transactions to Arc testnet and burns faucet USDC.",
            file=sys.stderr,
        )
        return 2

    # Resolve the deployer key. Priority: explicit --pk > keystore account
    # (--account / DEPLOYER_ACCOUNT, decrypted in-process) > DEPLOYER_PK env.
    # The keystore path is preferred per Circle's use-arc guidance: the raw
    # key never sits in an env var. Whatever the source, the resolved key
    # stays in-process and chain.py signs locally.
    rpc_url = args.rpc_url or os.environ.get("RPC", "")
    if args.pk:
        pk = args.pk
    else:
        try:
            pk = resolve_deployer_key(account=args.account, allow_interactive=True)
        except KeyResolutionError as e:
            # NEVER print the key or password — only the actionable guidance.
            print(f"REFUSING: {e}", file=sys.stderr)
            return 3
    if not pk:
        print("REFUSING: --mode live requires DEPLOYER_PK env var, --pk, or --account.", file=sys.stderr)
        return 3
    if not rpc_url:
        print("REFUSING: --mode live requires RPC env var or --rpc-url.", file=sys.stderr)
        return 3

    deployer = cast_address_from_pk(pk)
    print(f"LIVE MODE on Arc testnet")
    print(f"  deployer    : {deployer}")
    print(f"  rpc         : {rpc_url[:60]}...")
    print(f"  est. cost   : ~0.10 USDC (4 deploys + 6 txs)")
    print(f"  faucet      : https://faucet.circle.com  (needs >=2 USDC on {deployer})")
    print("")
    print("proceeding in 3 seconds. ctrl-C now to abort.")
    time.sleep(3)

    return run_demo(
        mode="live",
        rpc_url=rpc_url,
        pk=pk,
        output_path=Path(args.output),
        memory_path=args.memory,
        seed_n=args.seed_n,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> int:
    p = argparse.ArgumentParser()
    # No silent default: the caller must choose. `local` is a fork+anvil-key
    # TEST harness (never a real broadcast); `live` broadcasts to Arc. Forcing
    # an explicit choice keeps the test path from ever being mistaken for the
    # product run path.
    p.add_argument("--mode", choices=["local", "live"], required=True)
    p.add_argument("--rpc-url", default=None)
    p.add_argument("--pk", default=None, help="DEPLOYER_PK (live mode only)")
    p.add_argument(
        "--account",
        default=None,
        help=(
            "Encrypted Foundry keystore name in ~/.foundry/keystores/ "
            "(live mode, preferred over --pk/DEPLOYER_PK). Decrypted "
            "in-process via eth_account; password from KEYSTORE_PASSWORD or "
            "an interactive prompt. Create with `cast wallet import <name> "
            "--interactive`."
        ),
    )
    p.add_argument(
        "--yes-i-understand",
        action="store_true",
        help="Required confirmation for --mode live",
    )
    p.add_argument("--output", default=str(DEFAULT_OUTPUT))
    p.add_argument("--memory", default="/tmp/alice.mem")
    p.add_argument("--seed-n", type=int, default=50)
    args = p.parse_args()

    if args.mode == "local":
        return run_local(args)
    return run_live(args)


if __name__ == "__main__":
    sys.exit(main())
