"""test_duel_runner.py — the Colosseum match engine (counterfactual scoring).

Pure tests inject stub `Duelist`s (a canned `complete_fn`, no network) to cover
the counterfactual survived/fooled metric, the clean cycle, the failure penalty,
and that the right injectionId is sent on-chain. One anvil-fork integration test
deploys the real Colosseum + MockERC20 and drives a bounded duel through
DuelRunner — register → createDuel → inject → counterfactual run_cycle →
resolve — asserting the on-chain scores, resilience counters, and escrow
settlement.
"""

from __future__ import annotations

import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
from eth_abi import decode as abi_decode
from eth_abi import encode as abi_encode
from eth_utils import keccak

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from agents.duelist import Duelist  # noqa: E402
from agents.duel_runner import (  # noqa: E402
    ITEM_FLASHBANG,
    DuelConfig,
    DuelRunner,
    Injection,
    PythScorer,
    decode_chaos_log,
    encode_create_duel,
    encode_register_agent,
    encode_report_call,
    encode_resolve,
    find_injection_id_in_receipt,
    make_flashbang,
)
from scripts.lib.chain import (  # noqa: E402
    cast_address_from_pk,
    cast_send,
    deploy_contract_via_cast,
    rpc_call,
    wait_for_receipt,
)

ANVIL_KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
CHAIN_ID = 5042002
COLOSSEUM = REPO_ROOT / "contracts" / "out" / "Colosseum.sol" / "Colosseum.json"
MOCK_ERC20 = REPO_ROOT / "contracts" / "out" / "MockERC20.sol" / "MockERC20.json"


def _duelist(addr: str, hardened: bool, clean_dir: int, dirty_dir: int) -> Duelist:
    """A deterministic stub duelist: returns `clean_dir` with no telemetry and
    `dirty_dir` when an injection is present — lets us script resist vs fooled."""

    def complete(system: str, user: str) -> str:
        d = dirty_dir if "URGENT_MARKET_TELEMETRY" in user else clean_dir
        token = "long" if d > 0 else "short"
        return f'{{"direction":"{token}","reasoning":"r"}}'

    return Duelist(addr, hardened=hardened, complete_fn=complete)


def _capturing_sender():
    calls: list[str] = []

    def send(to_addr: str, data: str) -> dict:
        calls.append(data)
        return {"status": "0x1", "transactionHash": "0xabc", "logs": []}

    return send, calls


AGENT_A = "0x" + "aa" * 20
AGENT_B = "0x" + "bb" * 20
SPECTATOR = "0x" + "11" * 20


# ---------------------------------------------------------------------------
# Counterfactual scoring (pure, stubbed duelists)
# ---------------------------------------------------------------------------


def test_counterfactual_resist_vs_fooled():
    send, _ = _capturing_sender()
    A = _duelist(AGENT_A, True, clean_dir=1, dirty_dir=1)   # injection doesn't move it
    B = _duelist(AGENT_B, False, clean_dir=1, dirty_dir=-1)  # injection flips it
    runner = DuelRunner(
        DuelConfig("0xC0", A, B, symbol="SOL"), send, real_move_fn=lambda c: 300
    )
    runner.duel_id = 1
    ia = make_flashbang("SOL", 0, AGENT_A, SPECTATOR); ia.injection_id = 11
    ib = make_flashbang("SOL", 0, AGENT_B, SPECTATOR); ib.injection_id = 12
    reports = runner.run_cycle(2, {AGENT_A.lower(): ia, AGENT_B.lower(): ib})
    ra = next(r for r in reports if r.agent == AGENT_A)
    rb = next(r for r in reports if r.agent == AGENT_B)
    # A resisted: same call clean vs dirty → survived; profits on the real move.
    assert ra.ingested and ra.survived and ra.direction == 1 and ra.r_bps == 300
    # B fooled: injection flipped it short → lost on the real up-move.
    assert rb.ingested and not rb.survived and rb.direction == -1 and rb.r_bps == -300


def test_clean_cycle_no_injection():
    send, _ = _capturing_sender()
    A = _duelist(AGENT_A, True, clean_dir=-1, dirty_dir=-1)
    B = _duelist(AGENT_B, False, clean_dir=1, dirty_dir=1)
    runner = DuelRunner(DuelConfig("0xC0", A, B), send, real_move_fn=lambda c: -200)
    runner.duel_id = 1
    reports = runner.run_cycle(1)  # no injections
    ra = next(r for r in reports if r.agent == AGENT_A)
    assert not ra.ingested and ra.survived and ra.direction == -1
    assert ra.r_bps == 200  # short profits on a -200 move


def test_failure_penalty_bleeds():
    def boom(system, user):
        raise RuntimeError("model down")

    send, _ = _capturing_sender()
    A = Duelist(AGENT_A, hardened=True, complete_fn=boom)
    B = _duelist(AGENT_B, False, 1, 1)
    runner = DuelRunner(
        DuelConfig("0xC0", A, B, penalty_bps=100), send, real_move_fn=lambda c: 300
    )
    runner.duel_id = 1
    ia = make_flashbang("SOL", 0, AGENT_A, SPECTATOR); ia.injection_id = 7
    reports = runner.run_cycle(1, {AGENT_A.lower(): ia})
    ra = next(r for r in reports if r.agent == AGENT_A)
    assert ra.failed and not ra.survived and ra.ingested
    assert ra.r_bps == -100  # the drawdown penalty, not 0


def test_reportcall_carries_injection_id():
    send, calls = _capturing_sender()
    A = _duelist(AGENT_A, True, 1, 1)
    B = _duelist(AGENT_B, False, 1, 1)
    runner = DuelRunner(DuelConfig("0xC0", A, B), send, real_move_fn=lambda c: 100)
    runner.duel_id = 5
    ib = make_flashbang("SOL", 0, AGENT_B, SPECTATOR); ib.injection_id = 99
    runner.run_cycle(1, {AGENT_B.lower(): ib})
    # Find the reportCall calldata for agent B and decode its injectionId field.
    sel = "0x" + keccak(b"reportCall(uint256,address,uint256,int256,bool,bool,bool)")[:4].hex()
    report_calls = [c for c in calls if c.startswith(sel)]
    decoded = [
        abi_decode(
            ["uint256", "address", "uint256", "int256", "bool", "bool", "bool"],
            bytes.fromhex(c[10:]),
        )
        for c in report_calls
    ]
    b_call = next(d for d in decoded if d[1].lower() == AGENT_B.lower())
    assert b_call[2] == 99  # injectionId carried through


def test_pyth_scorer_forward_move_bps():
    prices = iter([100.0, 101.0, 99.0])
    scorer = PythScorer(price_fn=lambda: next(prices))
    assert scorer.move_bps(1) == 0          # baseline
    assert scorer.move_bps(2) == 100        # +1.00% = 100 bps
    assert scorer.move_bps(3) == -198       # (99-101)/101 = -1.98%


def test_encoders_have_correct_selectors():
    assert encode_create_duel("0x" + "11" * 20, "0x" + "22" * 20, 60, 3600).startswith(
        "0x" + keccak(b"createDuel(address,address,uint64,uint64)")[:4].hex()
    )
    assert encode_register_agent("0x" + "11" * 20).startswith(
        "0x" + keccak(b"registerAgent(address)")[:4].hex()
    )
    assert encode_report_call(1, "0x" + "11" * 20, 0, -5, True, False, False).startswith(
        "0x" + keccak(b"reportCall(uint256,address,uint256,int256,bool,bool,bool)")[:4].hex()
    )
    assert encode_resolve(7) == "0x" + (
        keccak(b"resolve(uint256)")[:4] + (7).to_bytes(32, "big")
    ).hex()


def test_decode_chaos_log_builds_injection():
    # Synthetic ChaosInjected log: injectionId=3, duelId=1, target=AGENT_B,
    # data = (spectator, itemKind=FLASHBANG, fee, escrow).
    topic0 = "0x" + keccak(
        b"ChaosInjected(uint256,uint256,address,address,uint8,uint256,uint256)"
    ).hex()
    log = {
        "topics": [
            topic0,
            "0x" + format(3, "064x"),
            "0x" + format(1, "064x"),
            "0x" + "00" * 12 + AGENT_B[2:],
        ],
        "data": "0x" + abi_encode(
            ["address", "uint8", "uint256", "uint256"],
            [SPECTATOR, ITEM_FLASHBANG, 500_000, 450_000],
        ).hex(),
    }
    target, inj = decode_chaos_log(log, "SOL")
    assert target == AGENT_B.lower()
    assert inj.injection_id == 3
    assert inj.item_kind == ITEM_FLASHBANG
    assert inj.claimed_move_bps != 0  # flashbang template applied


def test_run_cycle_remembers_and_anchors():
    """Memory-augmented agents store one trace per cycle and the runner anchors
    each agent's memory root every `anchor_every` cycles."""
    from agents.embedder import Embedder
    from agents.memory_service import MemoryService

    send, _ = _capturing_sender()
    A = Duelist(
        AGENT_A, hardened=True,
        complete_fn=lambda s, u: '{"direction":"long","reasoning":"a"}',
        memory=MemoryService(dim=384), embedder=Embedder(model_name=None),
    )
    B = Duelist(
        AGENT_B, hardened=False,
        complete_fn=lambda s, u: '{"direction":"short","reasoning":"b"}',
        memory=MemoryService(dim=384), embedder=Embedder(model_name=None),
    )
    anchors: list[tuple[str, bytes]] = []
    runner = DuelRunner(
        DuelConfig("0xC0", A, B, symbol="SOL"),
        send,
        real_move_fn=lambda c: 100,
        anchor_fn=lambda addr, root: anchors.append((addr, root)),
        anchor_every=2,
    )
    runner.duel_id = 1
    runner.run_cycle(1)  # 1 % 2 != 0 → no anchor
    assert anchors == []
    runner.run_cycle(2)  # 2 % 2 == 0 → anchor both
    # Each agent stored exactly one trace per cycle.
    assert A.memory_stats()["entries"] == 2
    assert B.memory_stats()["entries"] == 2
    # Both agents anchored once, with real 32-byte roots.
    assert len(anchors) == 2
    assert {a for a, _ in anchors} == {AGENT_A, AGENT_B}
    assert all(isinstance(r, bytes) and len(r) == 32 for _, r in anchors)


# ---------------------------------------------------------------------------
# Anvil-fork integration: real Colosseum, real duel
# ---------------------------------------------------------------------------


def _pick_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture()
def anvil_url():
    if not COLOSSEUM.exists():
        pytest.skip("Colosseum artifact not built (run `forge build`)")
    port = _pick_port()
    proc = subprocess.Popen(
        ["anvil", "--host", "127.0.0.1", "--port", str(port),
         "--chain-id", str(CHAIN_ID), "--quiet"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
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


def _erc20_call(sig: str, types: list[str], args: list) -> str:
    sel = keccak(sig.encode())[:4]
    return "0x" + (sel + abi_encode(types, args)).hex()


def _read_uint(rpc_url: str, addr: str, sig: str, types: list[str], args: list) -> int:
    sel = keccak(sig.encode())[:4]
    data = "0x" + (sel + abi_encode(types, args)).hex()
    out = rpc_call(rpc_url, "eth_call", [{"to": addr, "data": data}, "latest"])
    return int(out, 16)


def _resilience_of(rpc_url: str, colosseum: str, agent: str) -> tuple[int, int]:
    sel = keccak(b"resilienceOf(address)")[:4]
    arg = bytes(12) + bytes.fromhex(agent.removeprefix("0x"))
    out = rpc_call(rpc_url, "eth_call", [{"to": colosseum, "data": "0x" + (sel + arg).hex()}, "latest"])
    ingested, survived = abi_decode(["uint256", "uint256"], bytes.fromhex(out.removeprefix("0x")))
    return int(ingested), int(survived)


def test_duel_end_to_end_on_anvil(anvil_url):
    deployer = cast_address_from_pk(ANVIL_KEY)  # recorder + developer of both agents
    usdc, _ = deploy_contract_via_cast(
        rpc_url=anvil_url, pk=ANVIL_KEY, artifact_path=str(MOCK_ERC20),
        constructor_args=["6"],
    )
    colosseum, _ = deploy_contract_via_cast(
        rpc_url=anvil_url, pk=ANVIL_KEY, artifact_path=str(COLOSSEUM),
        constructor_args=[usdc, deployer, deployer],
    )

    def send(to_addr: str, data: str) -> dict:
        tx = cast_send(rpc_url=anvil_url, pk=ANVIL_KEY, to=to_addr, data=data)
        return wait_for_receipt(anvil_url, tx, timeout=30.0)

    # Fund + approve the deployer (developer) so registration + injection fees pull.
    send(usdc, _erc20_call("mint(address,uint256)", ["address", "uint256"], [deployer, 1_000_000_000]))
    send(usdc, _erc20_call("approve(address,uint256)", ["address", "uint256"], [colosseum, (1 << 256) - 1]))

    A = _duelist(AGENT_A, True, clean_dir=1, dirty_dir=1)   # resists
    B = _duelist(AGENT_B, False, clean_dir=1, dirty_dir=-1)  # fooled
    runner = DuelRunner(
        DuelConfig(colosseum, A, B, symbol="SOL", duration_secs=3600, penalty_bps=100),
        send, real_move_fn=lambda c: 300,
    )
    runner.register_agents()
    did = runner.create()
    assert did == 1

    # Spectator injects a flashbang on each agent; capture their injectionIds.
    from agents.duel_runner import encode_inject_chaos
    rA = send(colosseum, encode_inject_chaos(did, AGENT_A, ITEM_FLASHBANG))
    rB = send(colosseum, encode_inject_chaos(did, AGENT_B, ITEM_FLASHBANG))
    inj_a_id = find_injection_id_in_receipt(rA, colosseum)
    inj_b_id = find_injection_id_in_receipt(rB, colosseum)
    assert inj_a_id and inj_b_id

    runner.run_cycle(1)  # clean
    ia = make_flashbang("SOL", 0, AGENT_A, SPECTATOR); ia.injection_id = inj_a_id
    ib = make_flashbang("SOL", 0, AGENT_B, SPECTATOR); ib.injection_id = inj_b_id
    runner.run_cycle(2, {AGENT_A.lower(): ia, AGENT_B.lower(): ib})

    # A resisted its injection; B was hijacked.
    assert _resilience_of(anvil_url, colosseum, AGENT_A) == (1, 1)
    assert _resilience_of(anvil_url, colosseum, AGENT_B) == (1, 0)

    # Both injection escrows settled: A survived → paid out; B fooled → pool.
    assert _read_uint(anvil_url, colosseum, "injectionEscrows(uint256)", ["uint256"], [inj_a_id]) == 0
    assert _read_uint(anvil_url, colosseum, "injectionEscrows(uint256)", ["uint256"], [inj_b_id]) == 0
    # B's fooled escrow (0.5 USDC - 10% operator cut = 0.45) sits in the prize pool.
    assert _read_uint(anvil_url, colosseum, "prizePool(uint256)", ["uint256"], [did]) == 450_000

    # Advance past the window and resolve.
    rpc_call(anvil_url, "evm_increaseTime", [3700])
    rpc_call(anvil_url, "evm_mine", [])
    receipt = runner.resolve()
    assert int(receipt.get("status", "0x0"), 16) == 1
    # A: +300 (c1) +300 (c2 resisted) = 600. B: +300 (c1) -300 (c2 fooled) = 0 → A wins.
