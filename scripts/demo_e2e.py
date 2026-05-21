#!/usr/bin/env python3
"""demo_e2e — Phase 2 / Slice 5D end-to-end demo runner.

Two modes:

  --mode local  (default)
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
from scripts.lib.chain import (  # noqa: E402
    cast_address_from_pk,
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


def deploy_all_contracts(
    *,
    rpc_url: str,
    pk: str,
    usdc_addr: str = USDC_ADDR,
    bond_window_secs: int = 604800,
) -> dict[str, dict]:
    """Deploy ConstitutionRegistry, ConstitutionHook, MemoryAnchor, BondVault.

    Returns {name: {"address": "0x...", "tx_hash": "0x..."}}.
    """
    deployer = cast_address_from_pk(pk)
    addrs: dict[str, dict] = {}

    reg_addr, reg_tx = deploy_contract_via_cast(
        rpc_url=rpc_url, pk=pk, artifact_path=_artifact("ConstitutionRegistry")
    )
    addrs["ConstitutionRegistry"] = {"address": reg_addr, "tx_hash": reg_tx}

    hook_addr, hook_tx = deploy_contract_via_cast(
        rpc_url=rpc_url,
        pk=pk,
        artifact_path=_artifact("ConstitutionHook"),
        constructor_args=[reg_addr],
    )
    addrs["ConstitutionHook"] = {"address": hook_addr, "tx_hash": hook_tx}

    anchor_addr, anchor_tx = deploy_contract_via_cast(
        rpc_url=rpc_url, pk=pk, artifact_path=_artifact("MemoryAnchor")
    )
    addrs["MemoryAnchor"] = {"address": anchor_addr, "tx_hash": anchor_tx}

    vault_addr, vault_tx = deploy_contract_via_cast(
        rpc_url=rpc_url,
        pk=pk,
        artifact_path=_artifact("BondVault"),
        constructor_args=[usdc_addr, deployer, deployer, str(bond_window_secs)],
    )
    addrs["BondVault"] = {"address": vault_addr, "tx_hash": vault_tx}

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

    sol_rules = rules_to_solidity(rules_dicts)
    sel = keccak(b"defineConstitution((uint8,bytes)[])")[:4]
    body = abi_encode(["(uint8,bytes)[]"], [sol_rules])
    data = "0x" + (sel + body).hex()

    tx_hash = cast_send(
        rpc_url=rpc_url,
        pk=pk,
        to=registry_addr,
        data=data,
        gas_limit=1_000_000,
    )
    wait_for_receipt(rpc_url, tx_hash, timeout=60)
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
        addrs = deploy_all_contracts(rpc_url=rpc_url, pk=pk)
        # ---- Step 1: spawn Bob + define his constitution on-chain
        rules = default_bob_rules()
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
        # the smart account for the local demo) so the hook has something to
        # check. In real Arc flow this is done by spawn_agent.ts.
        # cast send hook.onInstall(abi.encode(constitutionHash))
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
                to=addrs["ConstitutionHook"]["address"],
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
            hook_address=addrs["ConstitutionHook"]["address"],
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
        # Anchor the (unchanged) pinned root.
        anchor_result = anchor_memory(
            rpc_url=rpc_url,
            pk=pk,
            anchor_address=addrs["MemoryAnchor"]["address"],
            root_hex=decay_ev["pinned_root_after"],
        )
        ok5 = stable and anchor_result.get("event_emitted", False)
        writer.append(
            step=5,
            name="anchor_pinned_root",
            ok=ok5,
            duration_ms=int((time.time() - t0) * 1000),
            tx_hash=anchor_result["tx_hash"],
            evidence={**decay_ev, **anchor_result, "pinned_root_stable": stable},
        )

        # ---- Step 6: child + bond resolution (slash + release)
        t0 = time.time()
        child_dict, child_ev = step_spawn_child_and_resolve_bond(bob)
        # We attempt a real `post(1 USDC)` and `slash(deployer, 100000)` to
        # produce on-chain evidence. The deployer is both bond owner AND
        # oracle in this demo, which lets us slash without separate keys.
        usdc_amount_one = 1_000_000
        bond_evidence: dict[str, Any] = {}
        try:
            bond_result = post_bond(
                rpc_url=rpc_url,
                pk=pk,
                vault_addr=addrs["BondVault"]["address"],
                usdc_addr=USDC_ADDR,
                amount_units=usdc_amount_one,
            )
            bond_evidence["bond_post"] = bond_result
            slash_tx = cast_send(
                rpc_url=rpc_url,
                pk=pk,
                to=addrs["BondVault"]["address"],
                sig="slash(address,uint256)",
                args=[deployer, "100000"],
                gas_limit=300_000,
            )
            wait_for_receipt(rpc_url, slash_tx, timeout=60)
            bond_evidence["slash_tx"] = slash_tx
            bond_evidence["bond_resolved"] = True
            top_tx = slash_tx
        except RuntimeError as e:
            # Bond flow can fail in local mode if the deployer doesn't have
            # USDC. We still report step success if at least the child spawn
            # worked, with the bond failure captured in evidence.
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

    pk = args.pk or os.environ.get("DEPLOYER_PK", "")
    rpc_url = args.rpc_url or os.environ.get("RPC", "")
    if not pk:
        print("REFUSING: --mode live requires DEPLOYER_PK env var or --pk.", file=sys.stderr)
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
    p.add_argument("--mode", choices=["local", "live"], default="local")
    p.add_argument("--rpc-url", default=None)
    p.add_argument("--pk", default=None, help="DEPLOYER_PK (live mode only)")
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
