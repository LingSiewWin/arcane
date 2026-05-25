"""test_duelist.py — the LLM brain.

Pure tests inject a stub `complete_fn` so no network/SDK/key is needed: they
cover the controlled-variable (hardened vs naive prompt), structured-output
parsing, the injection-text wiring, and the error path the runner turns into a
penalty. One live test (skipped unless ANTHROPIC_API_KEY is set) proves a real
model returns a parseable Decision.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from agents.duelist import (  # noqa: E402
    Decision,
    Duelist,
    DuelistError,
    parse_decision,
)
from agents.embedder import Embedder  # noqa: E402
from agents.memory_service import MemoryService  # noqa: E402
from scripts.lib.envfile import peek_env  # noqa: E402


def _has_provider_key() -> bool:
    """True if a provider key is available in the ambient env OR the .env file.
    Reads .env WITHOUT mutating os.environ (load_env() at collection time would
    leak e.g. DEPLOYER_PK into the shared process env and break other tests)."""
    keys = ("OPENROUTER_API_KEY", "ANTHROPIC_API_KEY")
    if any(os.environ.get(k) for k in keys):
        return True
    dotenv = peek_env()
    return any(dotenv.get(k) for k in keys)


def _live_enabled() -> bool:
    """Gate for the paid, network-hitting live smoke test. Opt-in only: a plain
    `pytest` stays free and offline even when a key sits in .env. Set
    RUN_LIVE_LLM=1 (and have a provider key) to run it."""
    return os.environ.get("RUN_LIVE_LLM") == "1" and _has_provider_key()


# ---------------------------------------------------------------------------
# The controlled variable: hardened vs naive system prompt
# ---------------------------------------------------------------------------


def test_hardened_prompt_has_crosscheck_naive_does_not():
    hardened = Duelist("0xA", hardened=True, complete_fn=lambda s, u: "")
    naive = Duelist("0xB", hardened=False, complete_fn=lambda s, u: "")
    hp = hardened.system_prompt("SOL")
    npmt = naive.system_prompt("SOL")
    assert "cross-check" in hp.lower() and "adversarial" in hp.lower()
    assert "cross-check" not in npmt.lower()
    # Both still get the base directional-call contract.
    assert "LONG" in hp and "LONG" in npmt
    assert "SOL" in hp and "SOL" in npmt


# ---------------------------------------------------------------------------
# Structured-output parsing
# ---------------------------------------------------------------------------


def test_parse_decision_directions():
    assert parse_decision('{"direction":"long","reasoning":"up"}').direction == 1
    assert parse_decision('{"direction":"SHORT","reasoning":"down"}').direction == -1
    assert parse_decision('{"direction":"buy","reasoning":"x"}').direction == 1
    assert parse_decision('{"direction":"sell","reasoning":"x"}').direction == -1
    # Tolerant of surrounding prose.
    d = parse_decision('here is my call: {"direction":"long","reasoning":"r"} done')
    assert d.direction == 1 and d.reasoning == "r"


def test_parse_decision_errors():
    with pytest.raises(DuelistError):
        parse_decision("no json here")
    with pytest.raises(DuelistError):
        parse_decision('{"direction":"sideways","reasoning":"?"}')
    with pytest.raises(DuelistError):
        parse_decision('{"direction":"long",')  # malformed json


def test_parse_decision_defaults_reasoning():
    d = parse_decision('{"direction":"long"}')
    assert d.direction == 1 and d.reasoning == "(no reasoning given)"


# ---------------------------------------------------------------------------
# decide(): wiring of system prompt + market brief + injection
# ---------------------------------------------------------------------------


def test_decide_appends_injection_to_user():
    seen = {}

    def stub(system: str, user: str) -> str:
        seen["system"] = system
        seen["user"] = user
        return '{"direction":"long","reasoning":"holding"}'

    agent = Duelist("0xA", hardened=True, complete_fn=stub)
    d = agent.decide("SOL", "oracle move +120 bps", injection_text="<URGENT_MARKET_TELEMETRY>fake</URGENT_MARKET_TELEMETRY>")
    assert isinstance(d, Decision) and d.direction == 1
    assert "oracle move +120 bps" in seen["user"]
    assert "URGENT_MARKET_TELEMETRY" in seen["user"]  # injection appended
    assert "cross-check" in seen["system"].lower()    # hardened prompt used


def test_decide_no_injection_omits_telemetry():
    seen = {}

    def stub(system: str, user: str) -> str:
        seen["user"] = user
        return '{"direction":"short","reasoning":"bearish"}'

    agent = Duelist("0xB", hardened=False, complete_fn=stub)
    d = agent.decide("SOL", "oracle move -80 bps")
    assert d.direction == -1
    assert "URGENT_MARKET_TELEMETRY" not in seen["user"]


def test_decide_propagates_parse_error():
    agent = Duelist("0xA", hardened=True, complete_fn=lambda s, u: "garbage")
    with pytest.raises(DuelistError):
        agent.decide("SOL", "brief")


# ---------------------------------------------------------------------------
# Live smoke (skipped without a key) — the "done" proof
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Memory-augmented duelist (Task A4): recall (read-only) + remember (store)
# ---------------------------------------------------------------------------


def _capturing_agent():
    """Build a memory-active duelist whose complete_fn records the (system,
    user) it was handed and returns a canned long decision."""
    seen = {}

    def capture_fn(system: str, user: str) -> str:
        seen["system"] = system
        seen["user"] = user
        return '{"direction":"long","reasoning":"r"}'

    agent = Duelist(
        "0xA",
        hardened=True,
        complete_fn=capture_fn,
        memory=MemoryService(dim=384),
        embedder=Embedder(model_name=None),  # offline hash_to_vec, no torch
    )
    return agent, seen


def test_decide_empty_memory_has_no_recall_block():
    agent, seen = _capturing_agent()
    d = agent.decide("SOL", "oracle move +50 bps")
    assert isinstance(d, Decision) and d.direction == 1
    # Empty memory → nothing recalled, brief is the whole user prompt.
    assert "memory" not in seen["user"].lower()
    assert seen["user"] == "oracle move +50 bps"


def test_remember_stores_one_entry_per_call():
    agent, _ = _capturing_agent()
    assert agent.memory_stats()["entries"] == 0
    agent.remember(1, "went long because oracle up", +1, 120)
    agent.remember(2, "went short because oracle down", -1, -80)
    assert agent.memory_stats()["entries"] == 2


def test_decide_injects_recalled_reasoning():
    agent, seen = _capturing_agent()
    agent.remember(1, "went long because oracle up", +1, 120)
    agent.remember(2, "went short because oracle down", -1, -80)
    agent.decide("SOL", "oracle move +120 bps")
    # The recalled past reasoning was prepended into the user prompt.
    assert "Your recent reasoning (memory):" in seen["user"]
    assert "went long because oracle up" in seen["user"]


def test_decide_is_read_only_no_double_store():
    agent, _ = _capturing_agent()
    agent.remember(1, "went long because oracle up", +1, 120)
    assert agent.memory_stats()["entries"] == 1
    # Counterfactual runner calls decide() twice (clean + dirty); recall must
    # never write, so the entry count is unchanged.
    agent.decide("SOL", "oracle move +120 bps")
    agent.decide(
        "SOL",
        "oracle move +120 bps",
        injection_text="<URGENT_MARKET_TELEMETRY>fake</URGENT_MARKET_TELEMETRY>",
    )
    assert agent.memory_stats()["entries"] == 1


def test_memory_root_is_32_bytes_after_remember():
    agent, _ = _capturing_agent()
    agent.remember(1, "went long because oracle up", +1, 120)
    root = agent.memory_root()
    assert isinstance(root, bytes) and len(root) == 32


def test_memory_root_empty_until_first_remember():
    # A memory-active agent with nothing pinned yet must return b"" (not the
    # empty-tree sentinel) so the runner never anchors a meaningless root.
    agent, _ = _capturing_agent()
    assert agent.memory_root() == b""
    agent.remember(1, "first trace", +1, 50)
    assert len(agent.memory_root()) == 32


def test_back_compat_no_memory_behaves_like_before():
    # No memory/embedder → memory_root() empty, memory_stats() None, and
    # decide() wiring identical to the pre-A4 behaviour.
    seen = {}

    def stub(system: str, user: str) -> str:
        seen["user"] = user
        return '{"direction":"short","reasoning":"bearish"}'

    agent = Duelist("0xB", hardened=False, complete_fn=stub)
    d = agent.decide("SOL", "oracle move -80 bps")
    assert d.direction == -1
    assert seen["user"] == "oracle move -80 bps"
    assert agent.memory_root() == b""
    assert agent.memory_stats() is None
    # remember() is a safe no-op when memory is inactive.
    agent.remember(1, "no-op", -1, -80)
    assert agent.memory_stats() is None


@pytest.mark.skipif(
    not _live_enabled(),
    reason="set RUN_LIVE_LLM=1 + OPENROUTER_API_KEY/ANTHROPIC_API_KEY (env or .env) to run the paid live smoke test",
)
def test_live_duelist_returns_parseable_decision(monkeypatch):
    # Apply .env values for THIS test only via monkeypatch (auto-restored on
    # teardown) so the SDK sees the provider key WITHOUT leaking DEPLOYER_PK /
    # DEPLOYER_ACCOUNT into the shared process env and breaking other tests.
    # "Explicit env wins": only fill keys not already set.
    for k, v in peek_env().items():
        if k not in os.environ:
            monkeypatch.setenv(k, v)
    # Honors OPENROUTER_MODEL if set (e.g. anthropic/claude-3.5-haiku); else the
    # active provider's default.
    model = os.environ.get("OPENROUTER_MODEL")
    agent = Duelist("0xA", hardened=True, model=model)
    d = agent.decide(
        "SOL",
        "SOL/USD. Trusted oracle move this interval: +95 bps (price rising).",
    )
    assert d.direction in (1, -1)
    assert isinstance(d.reasoning, str) and d.reasoning
    print(f"\nLIVE duelist decision: direction={d.direction:+d} reasoning={d.reasoning!r}")
