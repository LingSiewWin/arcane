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
from scripts.lib.envfile import load_env  # noqa: E402

# Load root .env so the live smoke test below can read OPENROUTER_API_KEY /
# ANTHROPIC_API_KEY from the file (no `export` needed) at collection time.
load_env()


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


@pytest.mark.skipif(
    not (os.environ.get("OPENROUTER_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")),
    reason="set OPENROUTER_API_KEY or ANTHROPIC_API_KEY to run the live duelist smoke test",
)
def test_live_duelist_returns_parseable_decision():
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
