"""test_arena.py — the arena matchmaker + cross-duel leaderboard (pure, no network).

Drives `Arena` over stubbed `Duelist`s with a fake `send_fn` that fabricates a
DuelCreated log (with an incrementing duelId) so `DuelRunner.create()` parses a
real id, and returns empty-log receipts for everything else. Asserts the pairing
(⌊N/2⌋ duels), the leaderboard aggregation (alpha = sum of direction*move*cycles),
both rankings, and — critically — that the Arena NEVER calls registerAgent.
"""

from __future__ import annotations

import sys
from pathlib import Path

from eth_abi import encode as abi_encode
from eth_utils import keccak

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from agents.arena import Arena, AgentStanding, ArenaResult  # noqa: E402
from agents.duelist import Duelist  # noqa: E402

COLOSSEUM = "0x" + "c0" * 20

_CREATE_SEL = "0x" + keccak(b"createDuel(address,address,uint64,uint64)")[:4].hex()
_REGISTER_SEL = "0x" + keccak(b"registerAgent(address)")[:4].hex()
_DUEL_CREATED_TOPIC0 = "0x" + keccak(
    b"DuelCreated(uint256,address,address,uint64,uint64,uint64)"
).hex()


def _addr_topic(addr: str) -> str:
    """ABI-pad a 20-byte address to a 32-byte indexed topic."""
    return "0x" + "00" * 12 + addr[2:].lower()


def _fake_sender():
    """A send_fn that mints an incrementing duelId via a DuelCreated log on every
    createDuel call, and returns a plain empty-log receipt for all other calls.
    Captures every calldata for later assertions (e.g. no registerAgent)."""
    calls: list[str] = []
    state = {"next_id": 1}

    def send(to_addr: str, data: str) -> dict:
        calls.append(data)
        if data.startswith(_CREATE_SEL):
            duel_id = state["next_id"]
            state["next_id"] += 1
            # createDuel args are (agentA, agentB, betting, trading); the first two
            # 32-byte ABI words are the agent addresses — reuse them as the event's
            # indexed agent topics so the log mirrors the real contract shape.
            body = data[10:]
            word_a = body[0:64]
            word_b = body[64:128]
            log = {
                "address": COLOSSEUM,
                "topics": [
                    _DUEL_CREATED_TOPIC0,
                    "0x" + format(duel_id, "064x"),
                    "0x" + word_a,
                    "0x" + word_b,
                ],
                "data": "0x",
            }
            return {
                "status": "0x1",
                "transactionHash": "0x" + format(duel_id, "064x"),
                "logs": [log],
            }
        return {"status": "0x1", "transactionHash": "0xab", "logs": []}

    return send, calls


def _stub_duelist(addr: str, hardened: bool, direction: int, persona: str) -> Duelist:
    """A canned duelist that always commits the same `direction` (no model call)."""
    token = "long" if direction > 0 else "short"
    payload = f'{{"direction":"{token}","reasoning":"{persona}"}}'
    return Duelist(
        addr,
        hardened=hardened,
        complete_fn=lambda system, user: payload,
        persona=persona,
    )


def _four_agents():
    a0 = "0x" + "a0" * 20
    a1 = "0x" + "a1" * 20
    a2 = "0x" + "a2" * 20
    a3 = "0x" + "a3" * 20
    # Two longs (+1) and two shorts (-1), distinct personas + hardening.
    d0 = _stub_duelist(a0, hardened=True, direction=1, persona="momentum")
    d1 = _stub_duelist(a1, hardened=False, direction=-1, persona="contrarian")
    d2 = _stub_duelist(a2, hardened=True, direction=1, persona="trend")
    d3 = _stub_duelist(a3, hardened=False, direction=-1, persona="fade")
    return [d0, d1, d2, d3], (a0, a1, a2, a3)


def test_arena_pairs_into_floor_n_over_2_duels():
    send, _ = _fake_sender()
    duelists, _ = _four_agents()
    result = Arena(COLOSSEUM, send, cycles=2, real_move_fn=lambda c: 100, sleep_fn=lambda s: None, duration_secs=0, settle_buffer_secs=0).run(duelists)
    assert isinstance(result, ArenaResult)
    # 4 agents → 2 duels, with incrementing ids minted by the fake sender.
    assert result.duel_ids == [1, 2]
    assert len(result.standings) == 4
    assert all(isinstance(s, AgentStanding) for s in result.standings)


def test_leaderboard_alpha_aggregation():
    send, _ = _fake_sender()
    duelists, (a0, a1, a2, a3) = _four_agents()
    # real_move = +100 each cycle, 2 cycles. alpha = direction * 100 * 2.
    result = Arena(COLOSSEUM, send, cycles=2, real_move_fn=lambda c: 100, sleep_fn=lambda s: None, duration_secs=0, settle_buffer_secs=0).run(duelists)
    by_addr = {s.address: s for s in result.standings}
    assert by_addr[a0].alpha_bps == 200    # long  → +1 * 100 * 2
    assert by_addr[a1].alpha_bps == -200   # short → -1 * 100 * 2
    assert by_addr[a2].alpha_bps == 200
    assert by_addr[a3].alpha_bps == -200
    # No injections were fed → nothing ingested, resilience defaults to 0.0.
    assert all(s.ingested == 0 and s.resilience == 0.0 for s in result.standings)


def test_alpha_ranking_longs_above_shorts():
    send, _ = _fake_sender()
    duelists, (a0, a1, a2, a3) = _four_agents()
    result = Arena(COLOSSEUM, send, cycles=2, real_move_fn=lambda c: 100, sleep_fn=lambda s: None, duration_secs=0, settle_buffer_secs=0).run(duelists)
    ranking = result.alpha_ranking()
    # Sorted by alpha desc; the two +200 longs lead the two -200 shorts.
    assert [s.alpha_bps for s in ranking] == [200, 200, -200, -200]
    leaders = {ranking[0].address, ranking[1].address}
    laggards = {ranking[2].address, ranking[3].address}
    assert leaders == {a0, a2}
    assert laggards == {a1, a3}


def test_arena_does_not_register_agents():
    send, calls = _fake_sender()
    duelists, _ = _four_agents()
    Arena(COLOSSEUM, send, cycles=2, real_move_fn=lambda c: 100, sleep_fn=lambda s: None, duration_secs=0, settle_buffer_secs=0).run(duelists)
    # The Arena must orchestrate create/report/resolve only — never registerAgent.
    assert not any(c.startswith(_REGISTER_SEL) for c in calls)
    # Sanity: it DID create the duels we expect.
    assert sum(1 for c in calls if c.startswith(_CREATE_SEL)) == 2


def test_odd_n_byes_last_agent():
    send, _ = _fake_sender()
    duelists, _ = _four_agents()
    fifth = _stub_duelist("0x" + "a4" * 20, hardened=True, direction=1, persona="solo")
    result = Arena(COLOSSEUM, send, cycles=1, real_move_fn=lambda c: 100, sleep_fn=lambda s: None, duration_secs=0, settle_buffer_secs=0).run(
        duelists + [fifth]
    )
    # 5 agents → floor(5/2)=2 duels; the 5th agent byes and never appears in standings.
    assert result.duel_ids == [1, 2]
    assert "0x" + "a4" * 20 not in {s.address for s in result.standings}
    assert len(result.standings) == 4


def test_shield_ranking_tiebreak_on_alpha():
    send, _ = _fake_sender()
    a0 = "0x" + "b0" * 20
    a1 = "0x" + "b1" * 20
    a2 = "0x" + "b2" * 20
    a3 = "0x" + "b3" * 20
    # Hand-build standings (unit-test the ranking helper directly): a0 & a1 both
    # full resilience but a0 has more alpha; a2 partial; a3 zero resilience.
    result = ArenaResult(
        duel_ids=[],
        standings=[
            AgentStanding(a2, alpha_bps=50, ingested=2, survived=1),   # res 0.5
            AgentStanding(a1, alpha_bps=10, ingested=2, survived=2),   # res 1.0
            AgentStanding(a0, alpha_bps=99, ingested=2, survived=2),   # res 1.0
            AgentStanding(a3, alpha_bps=80, ingested=2, survived=0),   # res 0.0
        ],
    )
    ranking = result.shield_ranking()
    assert [s.address for s in ranking] == [a0, a1, a2, a3]
    # a0 and a1 tie on resilience (1.0); alpha breaks it in a0's favour.
    assert ranking[0].resilience == 1.0 and ranking[1].resilience == 1.0
    assert ranking[0].alpha_bps > ranking[1].alpha_bps


def test_arena_polls_injections_and_scores_them():
    """The Arena polls on-chain for spectator chaos between cycles; a polled
    injection is fed to that cycle's run_cycle and shows up as ingested."""
    from agents.duel_runner import make_flashbang

    send, _ = _fake_sender()
    duelists, (a0, a1, a2, a3) = _four_agents()
    calls = {"n": 0}

    def poll(duel_id, from_block):
        calls["n"] += 1
        if calls["n"] == 1:  # first poll (duel 1, cycle 1) → hit a0
            return ({a0.lower(): make_flashbang("SOL", 0, a0, "0xSPEC")}, from_block + 1)
        return ({}, from_block + 1)

    result = Arena(
        COLOSSEUM, send, cycles=2, real_move_fn=lambda c: 100,
        sleep_fn=lambda s: None, duration_secs=0, settle_buffer_secs=0, poll_injections_fn=poll, cycle_interval_secs=1,
    ).run(duelists)
    by_addr = {s.address: s for s in result.standings}
    assert by_addr[a0].ingested >= 1  # the polled injection was scored
    assert calls["n"] >= 1            # the arena actually polled
