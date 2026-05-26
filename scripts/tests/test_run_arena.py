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


# ---------------------------------------------------------------------------
# Task 6 — fixed agent-pool REUSE: when --reuse-keystores is supplied, main()
# must load the existing pool and NEVER spawn or provision (zero USDC/gas spend).
# Fully offline: spawn/provision/assemble/arena.run are all faked; no network,
# no provider key beyond the env gate, no torch.
# ---------------------------------------------------------------------------


class _FakeResult:
    """Minimal stand-in for ArenaResult that _format_rankings can render."""

    duel_ids = [1, 2]

    def alpha_ranking(self):
        return []

    def shield_ranking(self):
        return []


class _FakeArena:
    def run(self, duelists):
        return _FakeResult()


def _set_live_env(monkeypatch):
    """Satisfy main()'s env gates without any real secret/network."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key-not-used-offline")
    monkeypatch.setenv("DEPLOYER_PK", "0x" + "11" * 32)
    monkeypatch.setenv("ARENA_RPC_URL", "http://offline.invalid")


def test_reuse_keystores_skips_spawn_and_provision(monkeypatch, tmp_path):
    """With --reuse-keystores, main() loads the pool and makes NO spawn/provision
    calls — the whole point of pool reuse (no USDC/gas, no provisioning gap)."""
    _set_live_env(monkeypatch)

    calls = {"spawn": 0, "provision": 0, "load": 0}

    def fake_load(*, keystore_dir=None, password=None):
        calls["load"] += 1
        assert str(keystore_dir) == str(tmp_path)
        return _wallets()  # 4 pre-provisioned wallets (all carry identity_id)

    def fake_spawn(*a, **k):
        calls["spawn"] += 1
        raise AssertionError("spawn_keypairs must NOT be called on the reuse path")

    def fake_provision(*a, **k):
        calls["provision"] += 1
        raise AssertionError("provision_agents must NOT be called on the reuse path")

    def fake_assemble(wallets, **kwargs):
        # Reuse path must hand the LOADED wallets straight to assembly.
        assert [w.address for w in wallets] == [w.address for w in _wallets()]
        return _FakeArena(), [object() for _ in wallets]

    monkeypatch.setattr(run_arena, "load_agent_wallets", fake_load)
    monkeypatch.setattr(run_arena, "spawn_keypairs", fake_spawn)
    monkeypatch.setattr(run_arena, "provision_agents", fake_provision)
    monkeypatch.setattr(run_arena, "assemble", fake_assemble)

    rc = run_arena.main([
        "--colosseum", "0xC0FFEE0000000000000000000000000000000001",
        "--memory-anchor", "0xA11CE00000000000000000000000000000000002",
        "--reuse-keystores", str(tmp_path),
    ])

    assert rc == 0
    assert calls["load"] == 1, "the reuse path must load the existing pool exactly once"
    assert calls["spawn"] == 0, "NO spawn on reuse"
    assert calls["provision"] == 0, "NO provision on reuse"


def test_fresh_path_spawns_and_provisions_and_persists_identities(monkeypatch, tmp_path):
    """Control: WITHOUT --reuse-keystores the fresh path spawns + provisions (and
    does NOT call load), and saves the identities sidecar so the pool is reusable."""
    _set_live_env(monkeypatch)

    calls = {"spawn": 0, "provision": 0, "load": 0, "save": 0}

    provisioned = _wallets()

    def fake_spawn(n, *a, **k):
        calls["spawn"] += 1
        return provisioned

    def fake_provision(wallets, **k):
        calls["provision"] += 1
        return list(wallets)

    def fake_load(*a, **k):
        calls["load"] += 1
        raise AssertionError("load_agent_wallets must NOT be called on the fresh path")

    def fake_save(wallets, **k):
        calls["save"] += 1
        return tmp_path / "identities.json"

    monkeypatch.setattr(run_arena, "spawn_keypairs", fake_spawn)
    monkeypatch.setattr(run_arena, "provision_agents", fake_provision)
    monkeypatch.setattr(run_arena, "load_agent_wallets", fake_load)
    monkeypatch.setattr(run_arena, "save_identities", fake_save)
    monkeypatch.setattr(run_arena, "assemble", lambda w, **kw: (_FakeArena(), list(w)))

    rc = run_arena.main([
        "--colosseum", "0xC0FFEE0000000000000000000000000000000001",
        "--memory-anchor", "0xA11CE00000000000000000000000000000000002",
        "--agents", "4",
    ])

    assert rc == 0
    assert calls["spawn"] == 1
    assert calls["provision"] == 1
    assert calls["save"] == 1, "the fresh pool's identities must be persisted for reuse"
    assert calls["load"] == 0
