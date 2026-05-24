"""arena_seed test — drive scripts.arena_seed.seed_arena against an anvil fork
and prove it really registers agents on chain.

Phase: AgoraHack "Agent Arena" live launcher. The live path (arena_live.sh →
scripts.arena_seed) mints a real ERC-8004 identity, posts a real bond, defines
a real constitution, then calls AgentRegistry.register — all against REAL Arc.
This test exercises the SAME register→agentCount path hermetically: a plain
anvil chain (no Arc broadcast, no key), with the identity-mint and bond-post
legs driven against mock identity/USDC contracts (the ERC-8004 register flow
and real USDC only exist on Arc) while the constitution + AgentRegistry legs
run against the REAL deployed contracts.

It asserts, after seeding 2 agents:
  * 2 AgentRegistered events were emitted (one per agent),
  * AgentRegistry.agentCount() == 2,
  * deployments/arena.json was written with 2 agents.

No mocks for AgentRegistry / ConstitutionRegistry / BondVault — those are the
real compiled contracts. Only the ERC-8004 identity (register/Registered) and
the native-USDC funding, which are Arc-only, are stood up as mocks so the test
needs neither a fork token nor faucet USDC. Never broadcasts to Arc.
"""

from __future__ import annotations

import json
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts.arena_seed import seed_arena  # noqa: E402
from scripts.lib.chain import (  # noqa: E402
    cast_address_from_pk,
    cast_call,
    cast_send,
    deploy_contract_via_cast,
    rpc_call,
    wait_for_receipt,
)
from eth_utils import keccak  # noqa: E402


ANVIL_KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
ONE_USDC = 1_000_000

OUT = REPO_ROOT / "contracts" / "out"
MOCK_ERC721 = OUT / "AgentRegistry.t.sol" / "MockERC721.json"
MOCK_ERC20 = OUT / "MockERC20.sol" / "MockERC20.json"
BOND_VAULT = OUT / "BondVault.sol" / "BondVault.json"
CONST_REGISTRY = OUT / "ConstitutionRegistry.sol" / "ConstitutionRegistry.json"
AGENT_REGISTRY = OUT / "AgentRegistry.sol" / "AgentRegistry.json"

_AGENT_REGISTERED_TOPIC = "0x" + keccak(
    b"AgentRegistered(uint256,uint256,address,bytes32)"
).hex()


pytestmark = pytest.mark.skipif(
    shutil.which("anvil") is None
    or shutil.which("forge") is None
    or shutil.which("cast") is None
    or not AGENT_REGISTRY.exists(),
    reason="foundry not on PATH or contracts not built (run `forge build`)",
)


def _pick_port(start=8810, end=8900) -> int:
    for p in range(start, end):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", p))
                return p
            except OSError:
                continue
    raise RuntimeError("no free port")


@pytest.fixture
def anvil_url():
    port = _pick_port()
    proc = subprocess.Popen(
        ["anvil", "--host", "127.0.0.1", "--port", str(port),
         "--chain-id", "5042002", "--quiet"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.time() + 10.0
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
        raise RuntimeError("anvil failed to start")
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()


def _count_agent_registered_events(rpc_url: str, registry_addr: str) -> int:
    logs = rpc_call(
        rpc_url,
        "eth_getLogs",
        [{"fromBlock": "0x0", "toBlock": "latest",
          "address": registry_addr, "topics": [_AGENT_REGISTERED_TOPIC]}],
    )
    return len(logs or [])


def test_seed_arena_registers_two_agents(anvil_url, tmp_path):
    deployer = cast_address_from_pk(ANVIL_KEY)

    # --- Deploy the REAL contracts the seeder talks to + Arc-only mocks. -----
    identity_addr, _ = deploy_contract_via_cast(
        rpc_url=anvil_url, pk=ANVIL_KEY, artifact_path=str(MOCK_ERC721)
    )
    usdc_addr, _ = deploy_contract_via_cast(
        rpc_url=anvil_url, pk=ANVIL_KEY,
        artifact_path=str(MOCK_ERC20), constructor_args=["6"],
    )
    vault_addr, _ = deploy_contract_via_cast(
        rpc_url=anvil_url, pk=ANVIL_KEY, artifact_path=str(BOND_VAULT),
        constructor_args=[usdc_addr, deployer, "604800", "259200"],
    )
    const_addr, _ = deploy_contract_via_cast(
        rpc_url=anvil_url, pk=ANVIL_KEY, artifact_path=str(CONST_REGISTRY)
    )
    agent_registry_addr, _ = deploy_contract_via_cast(
        rpc_url=anvil_url, pk=ANVIL_KEY, artifact_path=str(AGENT_REGISTRY),
        constructor_args=[identity_addr],
    )

    # Fund the deployer with USDC so the bond posts succeed (Arc gives this via
    # the native token; on a plain anvil we mint it on the mock).
    fund_tx = cast_send(
        rpc_url=anvil_url, pk=ANVIL_KEY, to=usdc_addr,
        sig="mint(address,uint256)", args=[deployer, str(10 * ONE_USDC)],
        gas_limit=200_000,
    )
    wait_for_receipt(anvil_url, fund_tx, timeout=30)

    # --- Mock the Arc-only legs (ERC-8004 register + native USDC). -----------
    # A counter that mints a distinct identity NFT to the deployer each call,
    # mirroring register_identity's "returns a fresh owned identity_id" contract.
    state = {"next_id": 1}

    def mint_identity() -> dict:
        identity_id = state["next_id"]
        state["next_id"] += 1
        tx = cast_send(
            rpc_url=anvil_url, pk=ANVIL_KEY, to=identity_addr,
            sig="mint(address,uint256)", args=[deployer, str(identity_id)],
            gas_limit=200_000,
        )
        wait_for_receipt(anvil_url, tx, timeout=30)
        return {"identity_id": identity_id, "register_tx": tx}

    def post_mock_bond() -> dict:
        approve_tx = cast_send(
            rpc_url=anvil_url, pk=ANVIL_KEY, to=usdc_addr,
            sig="approve(address,uint256)", args=[vault_addr, str(ONE_USDC)],
            gas_limit=200_000,
        )
        wait_for_receipt(anvil_url, approve_tx, timeout=30)
        post_tx = cast_send(
            rpc_url=anvil_url, pk=ANVIL_KEY, to=vault_addr,
            sig="post(uint256)", args=[str(ONE_USDC)], gas_limit=300_000,
        )
        wait_for_receipt(anvil_url, post_tx, timeout=30)
        return {"approve_tx": approve_tx, "post_tx": post_tx}

    out_json = tmp_path / "arena.json"
    summary = seed_arena(
        rpc_url=anvil_url,
        pk=ANVIL_KEY,
        registry_addr=agent_registry_addr,
        bond_vault=vault_addr,
        constitution_registry=const_addr,
        n=2,
        bond_usdc=1.0,
        write_path=out_json,
        identity_minter=mint_identity,
        bond_poster=post_mock_bond,
    )

    # --- Assertions: real on-chain proof, not the in-memory summary alone. ---
    assert len(summary["agents"]) == 2
    assert [a["agent_id"] for a in summary["agents"]] == [1, 2]
    # Distinct identities + distinct constitution hashes (distinct agents).
    ids = [a["identity_id"] for a in summary["agents"]]
    assert len(set(ids)) == 2, f"identities must be distinct, got {ids}"
    hashes = [a["constitution_hash"] for a in summary["agents"]]
    assert len(set(hashes)) == 2, f"constitutions must differ, got {hashes}"

    # 2 AgentRegistered events on chain.
    n_events = _count_agent_registered_events(anvil_url, agent_registry_addr)
    assert n_events == 2, f"expected 2 AgentRegistered events, got {n_events}"

    # AgentRegistry.agentCount() == 2 (real eth_call).
    count_out = cast_call(
        rpc_url=anvil_url, to=agent_registry_addr, sig="agentCount()(uint256)"
    )
    assert int(count_out.split()[0]) == 2, f"agentCount != 2: {count_out}"

    # deployments/arena.json was written with 2 agents.
    assert out_json.exists(), "arena.json must be written"
    written = json.loads(out_json.read_text())
    assert written["registry_addr"] == agent_registry_addr
    assert len(written["agents"]) == 2
    for a in written["agents"]:
        assert a["register_tx"].startswith("0x")
        assert a["constitution_hash"].startswith("0x")
