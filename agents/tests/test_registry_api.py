"""test_registry_api.py — real anvil-fork tests for the Agent Arena registry
API + the continuous AgentRunner (sub-project 2).

These tests are NOT mocked at the chain boundary. They:

  1. Spawn a local anvil fork (Arc chain id).
  2. Deploy the SAME contracts the real arena uses — MockERC721 (the ERC-8004
     identity stand-in compiled into AgentRegistry.t.sol), MockERC20 (the bond
     token), the real BondVault, and the real AgentRegistry.
  3. Mint identity NFTs, post real bonds, then drive the API + runner.
  4. Assert real ``AgentRegistered`` / ``AgentAction`` events in the receipts.

The API logic under test is real: ABI encoding, the kind->event mapping, the
directory assembly from ``agentCount`` + ``getAgent``, and the continuous
runner loop. Only the embedder uses the deterministic ``hash_to_vec`` fallback
(``embedding_model=None``) so the suite doesn't drag torch in — the SAME
fallback ``agents/bob.py`` ships, exercising the real MemoryService add/query.
"""

from __future__ import annotations

import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from agents.registry_api import (  # noqa: E402
    AdviceBody,
    RegisterBody,
    RegistryConfig,
    RegistryService,
    decode_advice_payload,
    encode_advice_payload,
    find_agent_action_in_receipt,
)
from eth_utils import keccak  # noqa: E402
from agents.agent_runner import AgentRunner, RunnerConfig  # noqa: E402
from scripts.lib.chain import (  # noqa: E402
    cast_address_from_pk,
    cast_send,
    deploy_contract_via_cast,
    wait_for_receipt,
)

ANVIL_KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
ANVIL_KEY_2 = "0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d"
CHAIN_ID = 5042002
ONE_USDC = 1_000_000

MOCK_ERC721 = REPO_ROOT / "contracts" / "out" / "AgentRegistry.t.sol" / "MockERC721.json"
MOCK_ERC20 = REPO_ROOT / "contracts" / "out" / "MockERC20.sol" / "MockERC20.json"
BOND_VAULT = REPO_ROOT / "contracts" / "out" / "BondVault.sol" / "BondVault.json"
AGENT_REGISTRY = REPO_ROOT / "contracts" / "out" / "AgentRegistry.sol" / "AgentRegistry.json"

pytestmark = pytest.mark.skipif(
    shutil.which("anvil") is None
    or shutil.which("forge") is None
    or shutil.which("cast") is None,
    reason="foundry not on PATH",
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
         "--chain-id", str(CHAIN_ID), "--quiet"],
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


def _deploy_arena(rpc_url: str, *, pk: str) -> dict:
    """Deploy the full arena stack on the fork and return the addresses.

    Mirrors AgentRegistry.t.sol's setUp: MockERC721 identity, MockERC20 bond
    token, the real BondVault, and the real AgentRegistry.
    """
    for art in (MOCK_ERC721, MOCK_ERC20, BOND_VAULT, AGENT_REGISTRY):
        assert art.exists(), f"artifact missing: {art}; run `forge build`"

    identity_addr, _ = deploy_contract_via_cast(
        rpc_url=rpc_url, pk=pk, artifact_path=str(MOCK_ERC721)
    )
    usdc_addr, _ = deploy_contract_via_cast(
        rpc_url=rpc_url, pk=pk, artifact_path=str(MOCK_ERC20),
        constructor_args=["6"],
    )
    # BondVault(bondToken, oracle, releaseWindow, livenessTimeout).
    vault_addr, _ = deploy_contract_via_cast(
        rpc_url=rpc_url, pk=pk, artifact_path=str(BOND_VAULT),
        constructor_args=[usdc_addr, "0x000000000000000000000000000000000000bEEF",
                          str(7 * 86400), str(3 * 86400)],
    )
    registry_addr, _ = deploy_contract_via_cast(
        rpc_url=rpc_url, pk=pk, artifact_path=str(AGENT_REGISTRY),
        constructor_args=[identity_addr],
    )
    return {
        "identity": identity_addr,
        "usdc": usdc_addr,
        "vault": vault_addr,
        "registry": registry_addr,
    }


def _mint_identity(rpc_url: str, pk: str, identity_addr: str, owner: str, token_id: int):
    tx = cast_send(rpc_url=rpc_url, pk=pk, to=identity_addr,
                   sig="mint(address,uint256)", args=[owner, str(token_id)])
    wait_for_receipt(rpc_url, tx, timeout=30)


def _post_bond(rpc_url: str, pk: str, usdc_addr: str, vault_addr: str,
               owner: str, amount: int):
    """Fund + post a real bond so BondVault.balanceOf(owner) > 0."""
    tx = cast_send(rpc_url=rpc_url, pk=pk, to=usdc_addr,
                   sig="mint(address,uint256)", args=[owner, str(amount)])
    wait_for_receipt(rpc_url, tx, timeout=30)
    tx = cast_send(rpc_url=rpc_url, pk=pk, to=usdc_addr,
                   sig="approve(address,uint256)", args=[vault_addr, str(amount)])
    wait_for_receipt(rpc_url, tx, timeout=30)
    tx = cast_send(rpc_url=rpc_url, pk=pk, to=vault_addr,
                   sig="post(uint256)", args=[str(amount)])
    wait_for_receipt(rpc_url, tx, timeout=30)


def _service(rpc_url: str, addrs: dict, *, pk: str = ANVIL_KEY) -> RegistryService:
    config = RegistryConfig(
        rpc_url=rpc_url,
        registry_addr=addrs["registry"],
        deployer_pk=pk,
        chain_id=CHAIN_ID,
        payment_recipient=cast_address_from_pk(pk),
        usdc_address=addrs["usdc"],
        embedding_model=None,  # deterministic hash_to_vec — no torch in tests.
    )
    return RegistryService(config)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_register_encodes_and_returns_agent_id(anvil_url):
    """/register returns agent_id 1 + a real tx; GET /agents then lists it."""
    deployer = cast_address_from_pk(ANVIL_KEY)
    addrs = _deploy_arena(anvil_url, pk=ANVIL_KEY)
    _mint_identity(anvil_url, ANVIL_KEY, addrs["identity"], deployer, 42)
    _post_bond(anvil_url, ANVIL_KEY, addrs["usdc"], addrs["vault"], deployer, 2 * ONE_USDC)

    svc = _service(anvil_url, addrs)
    out = svc.register_agent(RegisterBody(
        identity_id=42,
        constitution_hash="0x" + "11" * 32,
        dark_pool_url="https://alice.darkpool.example",
        bond_vault=addrs["vault"],
    ))
    assert out["agent_id"] == 1
    assert out["tx_hash"].startswith("0x") and len(out["tx_hash"]) == 66

    agents = svc.list_agents()
    assert len(agents) == 1
    a = agents[0]
    assert a["agent_id"] == 1
    assert a["identity_id"] == 42
    assert a["dark_pool_url"] == "https://alice.darkpool.example"
    assert a["operator"].lower() == deployer.lower()
    assert a["active"] is True
    assert a["reputation"] == {"wins": 0, "losses": 0}  # no oracle => honest 0/0


def test_advice_payload_roundtrips():
    """Pure unit test: the structured invocation-trace payload round-trips and
    its adviceHash is keccak(reasoning). No chain needed."""
    reasoning = "Momentum on SOL flipped positive after the 12-bar breakout."
    payload = encode_advice_payload(reasoning, "sol", "long")
    decoded = decode_advice_payload(payload)
    assert decoded is not None
    assert decoded["reasoning"] == reasoning
    assert decoded["symbol"] == "SOL"  # normalised upper-case
    assert decoded["stance"] == "long"
    assert decoded["advice_hash"] == "0x" + keccak(reasoning.encode()).hex()
    # Unknown stance falls back to neutral; legacy bare-hash decodes to None.
    assert decode_advice_payload(encode_advice_payload(reasoning, "ETH", "garbage"))[
        "stance"
    ] == "neutral"
    assert decode_advice_payload(keccak(reasoning.encode())) is None


def test_advice_records_action_event(anvil_url):
    """/agents/1/advice adds to memory + emits AgentAction(1, 0, ...) in logs."""
    deployer = cast_address_from_pk(ANVIL_KEY)
    addrs = _deploy_arena(anvil_url, pk=ANVIL_KEY)
    _mint_identity(anvil_url, ANVIL_KEY, addrs["identity"], deployer, 42)
    _post_bond(anvil_url, ANVIL_KEY, addrs["usdc"], addrs["vault"], deployer, ONE_USDC)

    svc = _service(anvil_url, addrs)
    svc.register_agent(RegisterBody(
        identity_id=42, constitution_hash="0x" + "11" * 32,
        dark_pool_url="https://a.example", bond_vault=addrs["vault"],
    ))

    before = len(svc.memory)
    reasoning = "SOL momentum is turning up; sizing in."
    out = svc.publish_advice(
        1, AdviceBody(trace=reasoning, symbol="SOL", stance="long")
    )
    assert len(svc.memory) == before + 1, "advice must land in the shared memory"

    # The recordAction receipt must carry a real AgentAction(1, 0, ...) event.
    receipt = wait_for_receipt(anvil_url, out["tx_hash"], timeout=30)
    event = find_agent_action_in_receipt(receipt, addrs["registry"])
    assert event is not None, "AgentAction event must be present"
    assert event["agent_id"] == 1
    assert event["kind"] == 0  # ADVICE_PUBLISHED
    # The endpoint also surfaces the decoded event directly.
    assert out["event"]["agent_id"] == 1
    assert out["event"]["kind"] == 0

    # The on-chain payload carries the FULL invocation trace (verifiable from
    # the event alone — this is what the UI trace drill-down decodes).
    trace = decode_advice_payload(event["payload"])
    assert trace is not None, "advice payload must decode to the structured trace"
    assert trace["reasoning"] == reasoning
    assert trace["symbol"] == "SOL"
    assert trace["stance"] == "long"
    assert trace["advice_hash"] == "0x" + keccak(reasoning.encode()).hex()


def test_directory_reads_chain(anvil_url):
    """Register 2 agents; GET /agents returns both with correct fields."""
    a1 = cast_address_from_pk(ANVIL_KEY)
    a2 = cast_address_from_pk(ANVIL_KEY_2)
    addrs = _deploy_arena(anvil_url, pk=ANVIL_KEY)

    _mint_identity(anvil_url, ANVIL_KEY, addrs["identity"], a1, 42)
    _mint_identity(anvil_url, ANVIL_KEY, addrs["identity"], a2, 7)
    _post_bond(anvil_url, ANVIL_KEY, addrs["usdc"], addrs["vault"], a1, ONE_USDC)
    _post_bond(anvil_url, ANVIL_KEY_2, addrs["usdc"], addrs["vault"], a2, ONE_USDC)

    svc1 = _service(anvil_url, addrs, pk=ANVIL_KEY)
    svc1.register_agent(RegisterBody(
        identity_id=42, constitution_hash="0x" + "aa" * 32,
        dark_pool_url="https://alice.example", bond_vault=addrs["vault"],
    ))
    svc2 = _service(anvil_url, addrs, pk=ANVIL_KEY_2)
    svc2.register_agent(RegisterBody(
        identity_id=7, constitution_hash="0x" + "bb" * 32,
        dark_pool_url="https://bob.example", bond_vault=addrs["vault"],
    ))

    agents = svc1.list_agents()
    assert len(agents) == 2
    by_id = {a["agent_id"]: a for a in agents}
    assert by_id[1]["identity_id"] == 42
    assert by_id[1]["dark_pool_url"] == "https://alice.example"
    assert by_id[1]["operator"].lower() == a1.lower()
    assert by_id[2]["identity_id"] == 7
    assert by_id[2]["dark_pool_url"] == "https://bob.example"
    assert by_id[2]["operator"].lower() == a2.lower()


def test_empty_directory_is_honest_empty_list(anvil_url):
    """No registrations => GET /agents is an empty list, never fabricated."""
    addrs = _deploy_arena(anvil_url, pk=ANVIL_KEY)
    svc = _service(anvil_url, addrs)
    assert svc.agent_count() == 0
    assert svc.list_agents() == []


def test_runner_emits_actions_continuously(anvil_url):
    """AgentRunner.run_n_cycles(3) emits >=3 AgentAction events — proving the
    continuous loop keeps the on-chain live feed flowing."""
    deployer = cast_address_from_pk(ANVIL_KEY)
    addrs = _deploy_arena(anvil_url, pk=ANVIL_KEY)
    _mint_identity(anvil_url, ANVIL_KEY, addrs["identity"], deployer, 42)
    _post_bond(anvil_url, ANVIL_KEY, addrs["usdc"], addrs["vault"], deployer, ONE_USDC)

    svc = _service(anvil_url, addrs)
    svc.register_agent(RegisterBody(
        identity_id=42, constitution_hash="0x" + "11" * 32,
        dark_pool_url="https://a.example", bond_vault=addrs["vault"],
    ))

    # No query signer wired (ARENA_QUERY_PK unset) and no oracle, so each cycle
    # emits exactly one AgentAction (the advice heartbeat). 3 cycles => >=3.
    runner = AgentRunner(svc, RunnerConfig(agent_ids=[1], record_query_actions=False))
    results = runner.run_n_cycles(3)

    assert len(results) == 3
    total_events = sum(r.action_events for r in results)
    assert total_events >= 3, f"expected >=3 AgentAction events, got {total_events}"
    # Every cycle published a fresh advice tx (distinct content each time).
    all_tx = [tx for r in results for tx in r.advice_tx]
    assert len(all_tx) == 3
    assert len(set(all_tx)) == 3, "each cycle must be a distinct on-chain tx"
    # The shared memory grew once per cycle (real adds, not no-ops).
    assert len(svc.memory) == 3
    # No errors on the happy path.
    assert all(not r.errors for r in results), [r.errors for r in results]


def test_runner_query_paid_action_emits_second_kind(anvil_url):
    """With record_query_actions on, each cycle emits BOTH an ADVICE_PUBLISHED
    and a QUERY_PAID action — 2 events/cycle, both real on-chain."""
    deployer = cast_address_from_pk(ANVIL_KEY)
    addrs = _deploy_arena(anvil_url, pk=ANVIL_KEY)
    _mint_identity(anvil_url, ANVIL_KEY, addrs["identity"], deployer, 42)
    _post_bond(anvil_url, ANVIL_KEY, addrs["usdc"], addrs["vault"], deployer, ONE_USDC)

    svc = _service(anvil_url, addrs)
    svc.register_agent(RegisterBody(
        identity_id=42, constitution_hash="0x" + "11" * 32,
        dark_pool_url="https://a.example", bond_vault=addrs["vault"],
    ))

    runner = AgentRunner(svc, RunnerConfig(agent_ids=[1], record_query_actions=True))
    results = runner.run_n_cycles(2)
    total_events = sum(r.action_events for r in results)
    # 2 cycles * (1 advice + 1 query) = 4 events.
    assert total_events == 4, f"expected 4 events, got {total_events}"
    assert all(len(r.query_tx) == 1 for r in results)


def test_runner_graceful_shutdown(anvil_url):
    """A stop flag (the SIGINT path) halts the loop cleanly mid-run."""
    deployer = cast_address_from_pk(ANVIL_KEY)
    addrs = _deploy_arena(anvil_url, pk=ANVIL_KEY)
    _mint_identity(anvil_url, ANVIL_KEY, addrs["identity"], deployer, 42)
    _post_bond(anvil_url, ANVIL_KEY, addrs["usdc"], addrs["vault"], deployer, ONE_USDC)

    svc = _service(anvil_url, addrs)
    svc.register_agent(RegisterBody(
        identity_id=42, constitution_hash="0x" + "11" * 32,
        dark_pool_url="https://a.example", bond_vault=addrs["vault"],
    ))

    runner = AgentRunner(svc, RunnerConfig(agent_ids=[1], record_query_actions=False))

    # Stop BEFORE running: run_n_cycles returns immediately with no cycles.
    runner.stop()
    assert runner.stopped is True
    results = runner.run_n_cycles(5)
    assert results == [], "a pre-set stop flag must halt the loop with zero cycles"

    # A fresh runner that stops after the first cycle (simulating SIGINT
    # arriving during the run) executes exactly one cycle, then exits clean.
    runner2 = AgentRunner(svc, RunnerConfig(agent_ids=[1], record_query_actions=False))
    orig = runner2.run_cycle

    def _cycle_then_stop():
        res = orig()
        runner2.stop()  # signal arrives after this cycle
        return res

    runner2.run_cycle = _cycle_then_stop  # type: ignore[method-assign]
    results2 = runner2.run_n_cycles(10, interval_secs=30.0)
    assert len(results2) == 1, "stop() must break the loop after the in-flight cycle"
    assert runner2.stopped is True
