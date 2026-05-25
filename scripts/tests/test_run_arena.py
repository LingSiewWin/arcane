"""test_run_arena — OFFLINE assembly-wiring test for scripts.run_arena.assemble.

Task A7. The live launcher (arena_live.sh -> scripts.run_arena.main) is
operator-gated: it broadcasts REAL Arc transactions and the duelists make REAL
model calls. This test exercises ONLY the pure assembly seam (`assemble`) with
every chain/price/embed dependency injected — NO network, NO provider key, NO
torch. It proves the wiring: N duelists built from N wallets, addresses matched,
personas assigned round-robin (distinct), hardened alternates, and the default
per-agent `anchor_fn` routes (address, root) to THAT wallet's own private key +
ERC-8004 identity_id.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from agents.agent_wallet import AgentWallet  # noqa: E402
from agents.arena import Arena  # noqa: E402
from agents.embedder import Embedder  # noqa: E402
from scripts import run_arena  # noqa: E402
from scripts.run_arena import assemble  # noqa: E402


PERSONAS = [
    "Persona-A: momentum",
    "Persona-B: mean-reversion",
    "Persona-C: contrarian",
    "Persona-D: breakout",
]


def _wallets() -> list[AgentWallet]:
    """Four fake wallets with distinct addresses, private keys, and identities."""
    return [
        AgentWallet(address="0x" + f"{i:040x}", private_key="0x" + f"{i:064x}",
                    identity_id=100 + i)
        for i in range(1, 5)
    ]


def _stub_embedder() -> Embedder:
    """Deterministic embedder (hash_to_vec path) — no torch, no model weights."""
    return Embedder(model_name=None)


def _assemble(wallets, **overrides):
    """Call assemble with offline fakes for every chain/price seam."""
    kwargs = dict(
        colosseum="0xC0FFEE0000000000000000000000000000000001",
        memory_anchor="0xA11CE00000000000000000000000000000000002",
        rpc_url="http://offline.invalid",
        operator_pk="0x" + "11" * 32,
        symbol="SOL",
        cycles=4,
        send_fn=lambda to, data: {"status": "0x1", "logs": []},
        anchor_fn=lambda addr, root: None,
        real_move_fn=lambda cycle: 0,
        embedder=_stub_embedder(),
        personas=PERSONAS,
    )
    kwargs.update(overrides)
    return assemble(wallets, **kwargs)


def test_assemble_builds_one_duelist_per_wallet_with_matching_addresses():
    wallets = _wallets()
    arena, duelists = _assemble(wallets)

    assert isinstance(arena, Arena)
    assert len(duelists) == 4
    assert [d.address for d in duelists] == [w.address for w in wallets]


def test_personas_assigned_round_robin_and_distinct():
    wallets = _wallets()
    _, duelists = _assemble(wallets)

    assigned = [d.persona for d in duelists]
    assert assigned == PERSONAS                  # round-robin, in order
    assert len(set(assigned)) == 4               # all distinct


def test_personas_wrap_round_robin_when_more_wallets_than_personas():
    wallets = _wallets() + _wallets()[:1]        # 5 wallets, 4 personas
    # the 5 wallets share addresses with the first set; give the 5th a fresh one
    wallets[4] = AgentWallet(address="0x" + f"{99:040x}",
                             private_key="0x" + f"{99:064x}", identity_id=199)
    _, duelists = _assemble(wallets)
    assert duelists[4].persona == PERSONAS[0]    # index 4 % 4 == 0


def test_hardened_alternates_across_the_field():
    wallets = _wallets()
    _, duelists = _assemble(wallets)
    assert [d.hardened for d in duelists] == [True, False, True, False]


def test_each_duelist_has_its_own_memory_and_the_shared_embedder():
    wallets = _wallets()
    emb = _stub_embedder()
    _, duelists = _assemble(wallets, embedder=emb)

    # One MemoryService PER agent (distinct instances), ONE shared embedder.
    mems = [d.memory for d in duelists]
    assert all(m is not None for m in mems)
    assert len({id(m) for m in mems}) == 4, "each agent must own a distinct MemoryService"
    assert all(d.embedder is emb for d in duelists), "the embedder must be shared"


def test_default_anchor_fn_routes_to_the_targeted_wallets_key_and_identity(monkeypatch):
    """The DEFAULT anchor_fn (anchor_fn=None) must, when called with a wallet's
    address + a 32-byte root, invoke anchor_memory with THAT wallet's own
    private_key + identity_id (agent-owned anchoring). We capture the call by
    injecting a fake anchor_memory."""
    wallets = _wallets()
    captured: list[dict] = []

    def fake_anchor_memory(**kwargs):
        captured.append(kwargs)
        return {"tx_hash": "0xdeadbeef", **kwargs}

    # run_arena.assemble imports anchor_memory lazily inside the function body
    # (`from scripts.anchor_memory import anchor_memory`), so patch it at source.
    monkeypatch.setattr("scripts.anchor_memory.anchor_memory", fake_anchor_memory)

    # anchor_fn=None forces the default per-agent anchor builder.
    arena, _ = _assemble(wallets, anchor_fn=None)
    default_anchor_fn = arena._anchor  # the seam DuelRunner receives

    target = wallets[2]                         # route to the 3rd wallet
    root = bytes(range(32))                     # 32-byte memory root
    default_anchor_fn(target.address, root)

    assert len(captured) == 1
    call = captured[0]
    assert call["pk"] == target.private_key, "must sign with the TARGET wallet's key"
    assert call["identity_id"] == target.identity_id, "must bind the TARGET's identity"
    assert call["anchor_address"] == "0xA11CE00000000000000000000000000000000002"
    assert call["root_hex"] == "0x" + root.hex()


def test_default_anchor_fn_is_case_insensitive_on_address(monkeypatch):
    """Routing must lowercase-match the address (Arena passes checksummed addrs)."""
    wallets = _wallets()
    captured: list[dict] = []
    monkeypatch.setattr(
        "scripts.anchor_memory.anchor_memory",
        lambda **kw: captured.append(kw) or {"tx_hash": "0x1"},
    )
    arena, _ = _assemble(wallets, anchor_fn=None)

    target = wallets[1]
    arena._anchor(target.address.upper(), bytes(32))
    assert captured[0]["pk"] == target.private_key


def test_default_personas_used_when_none_passed():
    wallets = _wallets()
    _, duelists = _assemble(wallets, personas=None)
    assigned = [d.persona for d in duelists]
    assert assigned == list(run_arena.DEFAULT_PERSONAS)
    assert len(set(assigned)) == 4


def test_arena_carries_injected_symbol_and_cycles():
    wallets = _wallets()
    arena, _ = _assemble(wallets, symbol="eth", cycles=7)
    assert arena.symbol == "eth"
    assert arena.cycles == 7
