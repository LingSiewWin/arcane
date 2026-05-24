"""orchestrator — drives the 6-step Constrained Cognition demo (Slice 5A).

Wires Alice + Bob together and exposes one entrypoint per spec §4.9 step.

Two surfaces:

1. **Class API (Slice 5A primary)** — the brief's contract.

       from agents.alice import Alice
       from agents.bob import Bob
       from agents.orchestrator import Orchestrator

       alice = Alice(); alice.bootstrap()
       bob = Bob(budget_usdc=10.0, constitution_rules=[...]); bob.bootstrap()
       orch = Orchestrator(alice, bob)
       r1 = orch.run_demo_step(1)
       ...
       r6 = orch.run_demo_step(6)

   Each ``run_demo_step(n)`` returns:

       {
           "step": n,
           "name": "Bob queries Alice's dark pool",
           "ok": True,
           "duration_ms": 42.3,
           "evidence": {...},
           "next_step_hint": "...",
       }

   Step 4 prepares (but does NOT broadcast) a ``TradeIntent`` whose calldata
   targets the REAL rules in Slice 2's ConstitutionHook — MAX_TRADE_SIZE via
   ERC-20 ``transfer(address,uint256)``, NOT the made-up ``setLeverage``.
   Slice 5D owns broadcast.

2. **Module helpers (preserved compatibility)** — earlier Slice-5D stub's
   ``step_*`` functions. Kept so ``scripts/demo_e2e.py`` continues to import
   them without churn.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import numpy as np

from agents.alice import Alice
from agents.bob import (
    Bob,
    TradeIntent,
    build_erc20_transfer_calldata,
    build_execute_calldata,
    constitution_hash as bob_constitution_hash,
    rules_to_solidity,
)
from agents.memory_service import MemoryService

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Default constitution — three demo rules that fire on REAL selectors.
# The 2x leverage rule remains here for the constitution hash but uses
# Slice 2's made-up `setLeverage` selector and therefore doesn't fire on
# Bob's actual trade path (documented limitation, see docs/audit_phase1.md).
# ---------------------------------------------------------------------------


def default_bob_rules() -> list[dict]:
    """Three demo rules as Slice 5A's Bob expects them.

    Shape matches ``agents.bob.rules_to_solidity``: dict with ``kind`` +
    kind-specific params.
    """
    return [
        # Informational only — Bob doesn't emit setLeverage() calldata in the
        # demo flow. Kept so the on-chain constitution hash matches what
        # `contracts/test/ConstitutionHook.t.sol` exercises.
        {"rule_id": "MAX_LEV_2X", "kind": "MAX_LEVERAGE", "max_leverage_bps": 20000},
        # Load-bearing for the demo — ERC-20 transfer >1 USDC reverts.
        {"rule_id": "MAX_TRADE_1USDC", "kind": "MAX_TRADE_SIZE", "max_usdc": 1.0},
        # Sentinel blacklisted venue.
        {
            "rule_id": "VENUE_BLACKLIST_DEAD",
            "kind": "VENUE_BLACKLIST",
            "venues": ["0x000000000000000000000000000000000000dEaD"],
        },
    ]


# Alias preserved from earlier stub; Bob takes the dict form.
DEFAULT_BOB_RULES: list[dict] = default_bob_rules()

DEFAULT_MARKET_STATE = (
    "ETH funding flipped negative, perp open interest jumped 40%, "
    "considering short on Hyperliquid"
)


def hash_constitution(rules: list[dict] | list[tuple[int, bytes]]) -> str:
    """``keccak256(abi.encode(Rule[]))`` — matches ConstitutionRegistry.hashOf.

    Accepts either dict-rules (Bob's shape) or already-Solidity tuples.
    """
    if rules and isinstance(rules[0], dict):
        return bob_constitution_hash(rules)  # type: ignore[arg-type]
    from eth_abi import encode as abi_encode
    from eth_utils import keccak

    # Solidity Rule is (uint8 kind, bytes params, address adapter) — Phase 5
    # Stream M. Tuples passed here are expected to already carry the adapter.
    encoded = abi_encode(["(uint8,bytes,address)[]"], [rules])
    return "0x" + keccak(encoded).hex()


# ===========================================================================
# Orchestrator class — primary Slice 5A surface.
# ===========================================================================


@dataclass
class Orchestrator:
    """Wires Alice + Bob and runs the 6-step demo."""

    alice: Alice
    bob: Bob
    market_state: str = DEFAULT_MARKET_STATE

    # State captured across run_demo_step() calls.
    last_query_results: list[dict] = field(default_factory=list, init=False)
    last_intent: Optional[TradeIntent] = field(default=None, init=False)
    last_child: Optional[Bob] = field(default=None, init=False)

    def run_demo_step(self, step_no: int) -> dict:
        """Execute one step and return a structured evidence dict.

        Steps 1..6 follow spec §4.9. Each is idempotent within an instance
        (calling step 2 twice pays twice + returns two result sets); the
        instance carries `last_query_results` / `last_intent` / `last_child`
        across calls so step N can lean on what step N-1 produced.
        """
        runners = {
            1: self._step1_spawn_bob,
            2: self._step2_query_alice,
            3: self._step3_record_lesson,
            4: self._step4_attempt_violating_trade,
            5: self._step5_decay_memory,
            6: self._step6_spawn_child,
        }
        if step_no not in runners:
            raise ValueError(f"step_no must be 1..6, got {step_no}")
        t0 = time.time()
        try:
            payload = runners[step_no]()
        except Exception as exc:  # noqa: BLE001
            log.exception("step %s raised", step_no)
            return {
                "step": step_no,
                "name": _STEP_NAMES[step_no],
                "ok": False,
                "duration_ms": (time.time() - t0) * 1000,
                "evidence": {"error": repr(exc)},
                "next_step_hint": "infrastructure failure — inspect error",
            }
        payload.setdefault("step", step_no)
        payload.setdefault("name", _STEP_NAMES[step_no])
        payload.setdefault("ok", True)
        payload.setdefault("duration_ms", (time.time() - t0) * 1000)
        payload.setdefault("next_step_hint", _NEXT_HINTS.get(step_no, ""))
        return payload

    # ---- Step 1: spawn Bob (EOA + constitution) -----------------------

    def _step1_spawn_bob(self) -> dict:
        if not self.alice.bootstrapped:
            self.alice.bootstrap()
        if self.bob.eoa is None:
            self.bob.bootstrap()
        return {
            "evidence": {
                "bob_eoa": self.bob.address,
                "bob_budget_usdc": self.bob.budget_usdc,
                "constitution_hash": self.bob.constitution_hash,
                "rule_kinds": [r["kind"] for r in self.bob.constitution_rules],
                "alice_address": self.alice.address,
                "alice_pinned_root": "0x" + self.alice.pinned_root.hex(),
                "alice_entries": (
                    len(self.alice.memory) if self.alice.memory else 0
                ),
            },
        }

    # ---- Step 2: Bob queries Alice's dark pool (x402) -----------------

    def _step2_query_alice(self) -> dict:
        if not self.alice.bootstrapped:
            self.alice.bootstrap()
        if self.bob.eoa is None:
            self.bob.bootstrap()

        # Count x402 payments processed via the dark pool's nonce store.
        # The store grows by exactly one if the dance completed correctly.
        nonce_store = self.alice.server._nonce_store  # type: ignore[attr-defined]
        nonces_before = (
            len(nonce_store) if hasattr(nonce_store, "__len__") else 0
        )

        results = self.bob.query_alice(
            alice_url="",
            market_state=self.market_state,
            k=5,
            chain_id=self.alice.chain_id,
            asset_address=self.alice.usdc_address,
            expected_price_usdc=self.alice.price_usdc,
            expected_recipient=self.alice.payment_recipient,
            transport=self.alice.client,
        )
        self.last_query_results = results
        nonces_after = (
            len(nonce_store) if hasattr(nonce_store, "__len__") else 0
        )

        top = results[0] if results else None
        return {
            "evidence": {
                "query_text": self.market_state,
                "result_count": len(results),
                "top_result_trace_id": (top or {}).get("trace_id"),
                "top_result_score": (top or {}).get("score"),
                "top_result_text": ((top or {}).get("payload") or {}).get("text"),
                "x402_payments_recorded": nonces_after - nonces_before,
                "alice_recipient": self.alice.payment_recipient,
                "x402_price_units": self.alice.server.price_units,  # type: ignore[union-attr]
            },
        }

    # ---- Step 3: Bob records the lesson in his own memory ------------

    def _step3_record_lesson(self) -> dict:
        if not self.last_query_results:
            # Allow steps to be called out of order — auto-run step 2.
            self._step2_query_alice()
        if not self.last_query_results:
            raise RuntimeError("dark pool returned no results — cannot record")

        from agents.bob import _parse_trace_text  # local import; tiny private fn

        top = self.last_query_results[0]
        tid = top.get("trace_id")
        text = (top.get("payload") or {}).get("text", "")
        parsed = _parse_trace_text(text)
        n_before = len(self.bob.memory) if self.bob.memory else 0
        self.bob._remember_trace(tid, text, parsed)
        n_after = len(self.bob.memory) if self.bob.memory else 0
        return {
            "evidence": {
                "recorded_trace_id": tid,
                "recorded_text": text,
                "parsed": parsed,
                "bob_memory_entries_before": n_before,
                "bob_memory_entries_after": n_after,
                "pinned_in_bob": (
                    self.bob.memory.pinned_ids() if self.bob.memory else []
                ),
            },
        }

    # ---- Step 4: prepare (NOT broadcast) a violating TradeIntent -----

    def _step4_attempt_violating_trade(self) -> dict:
        """Builds a TradeIntent that violates MAX_TRADE_SIZE.

        We force a trade size 5x above the cap, encoded as an ERC-20
        ``transfer(address,uint256)`` wrapped in ``execute(target, value,
        data)``. That fires Slice 2's REAL ``transfer`` selector check —
        NOT the made-up ``setLeverage``.
        """
        if not self.alice.bootstrapped:
            self.alice.bootstrap()
        if self.bob.eoa is None:
            self.bob.bootstrap()
        if not self.last_query_results:
            self._step2_query_alice()

        cap_usdc = _max_trade_cap_usdc(self.bob.constitution_rules) or 1.0
        oversized = cap_usdc * 5.0

        intent = self.bob.decide(
            alice_url="",
            market_state=self.market_state,
            transport=self.alice.client,
            chain_id=self.alice.chain_id,
            asset_address=self.alice.usdc_address,
            expected_price_usdc=self.alice.price_usdc,
            expected_recipient=self.alice.payment_recipient,
            trade_size_usdc=oversized,
        )
        self.last_intent = intent

        return {
            "evidence": {
                "intent_kind": intent.kind,
                "intent_target": intent.target,
                "intent_value": intent.value,
                "inner_selector": intent.selector_hex(),
                "inner_calldata_hex": intent.calldata_hex(),
                "execute_calldata_hex": intent.execute_calldata_hex(),
                "expected_revert_reason": "ConstitutionViolation:MAX_TRADE_SIZE",
                "trade_size_usdc": oversized,
                "constitution_cap_usdc": cap_usdc,
                "notes": intent.notes,
                "broadcast_handoff": "Slice 5D",
            },
            "next_step_hint": (
                "Slice 5D: submit execute_calldata_hex to "
                "ConstitutionHook.validateUserOp; expect "
                "ConstitutionViolation:MAX_TRADE_SIZE revert"
            ),
        }

    # ---- Step 5: decay; pinned constitution survives -----------------

    def _step5_decay_memory(self) -> dict:
        if self.bob.memory is None:
            self.bob.bootstrap()
        mem = self.bob.memory
        if mem is None:
            raise RuntimeError("Bob has no memory after bootstrap — fatal")

        # Pad working entries so decay has clear evictions to demonstrate.
        rng = np.random.default_rng(self.bob.seed)
        for i in range(20):
            v = rng.standard_normal(mem.dim).astype(np.float32)
            mem.add(
                trace_id=f"working:padding_{i:03d}",
                vec=v,
                kind="working",
                payload={"i": i},
            )

        n_before = len(mem)
        pinned_before = mem.pinned_ids()
        root_before = mem.pinned_merkle_root().hex()

        # Advance time hard. lambda_working = 1/86400 per second → 30 days
        # gives weight ≈ exp(-30) ≈ 9e-14, far below the eviction threshold.
        far_future = time.time() + 30 * 86400
        mem.decay_step(now=far_future)

        n_after = len(mem)
        pinned_after = mem.pinned_ids()
        root_after = mem.pinned_merkle_root().hex()
        return {
            "evidence": {
                "entries_before": n_before,
                "entries_after": n_after,
                "evicted": max(0, n_before - n_after),
                "pinned_before": pinned_before,
                "pinned_after": pinned_after,
                "pinned_root_before": "0x" + root_before,
                "pinned_root_after": "0x" + root_after,
                "pinned_root_stable": root_before == root_after,
                "advance_seconds": 30 * 86400,
            },
            "ok": (
                root_before == root_after and pinned_before == pinned_after
            ),
        }

    # ---- Step 6: spawn child agent with inherited constitution ------

    def _step6_spawn_child(self) -> dict:
        if self.bob.eoa is None:
            self.bob.bootstrap()
        child = self.bob.spawn_child(
            child_budget_usdc=self.bob.budget_usdc / 2,
        )
        self.last_child = child
        return {
            "evidence": {
                "parent_eoa": self.bob.address,
                "child_eoa": child.address,
                "parent_budget_usdc": self.bob.budget_usdc,
                "child_budget_usdc": child.budget_usdc,
                "parent_constitution_hash": self.bob.constitution_hash,
                "child_constitution_hash": child.constitution_hash,
                "constitution_inherited": (
                    child.constitution_hash == self.bob.constitution_hash
                ),
                "note": (
                    "Real ERC-7715 session-key issuance is Slice 5C; here "
                    "we materialise the child agent in-process only."
                ),
            },
        }


_STEP_NAMES: dict[int, str] = {
    1: "Spawn Bob (EOA + constitution)",
    2: "Bob queries Alice's dark pool (x402)",
    3: "Bob writes the new lesson to his own memory",
    4: "Bob attempts a constitution-violating trade (intent ready)",
    5: "Bob's memory decays; pinned constitution rules survive",
    6: "Bob spawns a child agent with inherited constitution",
}

_NEXT_HINTS: dict[int, str] = {
    1: "step 2: pay 0.001 USDC, query Alice for the top reasoning trace",
    2: "step 3: record the trace as a new working-memory lesson",
    3: "step 4: prepare a trade that violates MAX_TRADE_SIZE → expect revert",
    4: "step 5: decay working memory; pinned constitution rules must survive",
    5: "step 6: spawn a child agent with half-budget + same constitution",
    6: "demo complete — Slice 5D broadcasts the step-4 intent on Arc",
}


def _max_trade_cap_usdc(rules: list[dict]) -> Optional[float]:
    for r in rules:
        if r.get("kind") == "MAX_TRADE_SIZE":
            return float(r.get("max_usdc", 0.0))
    return None


# ===========================================================================
# Standalone step helpers — earlier Slice-5D stub interface, preserved so
# ``scripts/demo_e2e.py`` keeps working without churn.
# ===========================================================================


def step_spawn_bob(
    *,
    budget_usdc: float = 10.0,
    rules: Optional[list[dict]] = None,
) -> tuple[Bob, dict]:
    rules = rules or default_bob_rules()
    bob = Bob(budget_usdc=budget_usdc, constitution_rules=rules)
    bob.bootstrap()
    return bob, {
        "eoa": bob.address,
        "budget_usdc": budget_usdc,
        "constitution_hash": bob.constitution_hash,
        "rule_kinds": [r["kind"] for r in rules],
    }


def step_query_alice(
    bob: Bob,
    alice_url: str,
    prompt: str = "long ETH on Drift size 50 USDC because funding flipped negative",
    *,
    transport=None,
    chain_id: int = 5042002,
    asset_address: str = "0x3600000000000000000000000000000000000000",
) -> tuple[list[dict], dict]:
    results = bob.query_alice(
        alice_url=alice_url,
        market_state=prompt,
        k=5,
        chain_id=chain_id,
        asset_address=asset_address,
        transport=transport,
    )
    return results, {
        "alice_url": alice_url,
        "prompt": prompt,
        "result_count": len(results),
        "top_trace_id": results[0].get("trace_id") if results else None,
    }


def step_select_violating_trace(
    results: list[dict],
) -> tuple[Optional[dict], dict]:
    """Pick the first trace whose payload implies an oversized trade (>1 USDC).
    Falls back to top-1 so the demo always has *something* downstream.
    """
    chosen = None
    interp = "no obvious violator; using top-1"
    for r in results:
        text = ((r.get("payload") or {}).get("text") or "")
        if any(s in text for s in ("size 50", "size 100", "size 250", "size 500", "size 1000")):
            chosen = r
            interp = "oversized trade — will trip MAX_TRADE_SIZE rule"
            break
    if chosen is None and results:
        chosen = results[0]
    return chosen, {
        "selected": chosen.get("trace_id") if chosen else None,
        "selected_score": chosen.get("score") if chosen else None,
        "selected_text": ((chosen or {}).get("payload") or {}).get("text"),
        "interpretation": interp,
    }


def step_attempt_violating_trade(
    bob: Bob,
    *,
    rpc_url: str,
    hook_address: str,
    deployer_pk: str,
    sca_address: Optional[str] = None,
    oversize_usdc: float = 50.0,
) -> tuple[bool, dict]:
    """Build oversized transfer + submit to the hook (deferred broadcast).

    The actual broadcast helper lives in ``scripts.lib.chain`` (Slice 5D
    territory). If that module is absent the function returns
    ``(False, {"intent_ready": True, ...})`` instead of raising — the
    intent itself is the load-bearing artifact for unit tests.
    """
    from agents.bob import _usdc_to_units  # local import

    sca = sca_address or bob.address
    usdc_target = "0x3600000000000000000000000000000000000000"
    recipient = "0x000000000000000000000000000000000000bEEF"
    amount_units = _usdc_to_units(oversize_usdc)
    inner = build_erc20_transfer_calldata(recipient, amount_units)
    outer = build_execute_calldata(usdc_target, 0, inner)
    outer_hex = "0x" + outer.hex()

    base_evidence = {
        "expected_rule": "MAX_TRADE_SIZE",
        "oversize_usdc": oversize_usdc,
        "amount_units": amount_units,
        "inner_selector": "0xa9059cbb (transfer)",
        "execute_calldata_hex": outer_hex,
    }

    try:
        from scripts.lib.chain import call_validate_user_op_expect_revert  # type: ignore
    except ImportError:
        return False, {**base_evidence, "intent_ready": True,
                        "broadcast_handoff": "Slice 5D"}

    revert_seen, evidence = call_validate_user_op_expect_revert(
        rpc_url=rpc_url,
        hook_address=hook_address,
        sender=sca,
        callData=outer_hex,
        deployer_pk=deployer_pk,
    )
    evidence.update(base_evidence)
    return revert_seen, evidence


def step_decay_check_pinned(
    memory_path: str,
    *,
    advance_seconds: float = 86400 * 30,
) -> tuple[bool, dict]:
    """Load Alice's memory; decay; assert pinned root is stable."""
    mem = MemoryService.load(memory_path)
    root_before = mem.pinned_merkle_root().hex()
    n_before = _entry_count(mem)

    now = time.time() + advance_seconds
    mem.decay_step(now=now)

    root_after = mem.pinned_merkle_root().hex()
    n_after = _entry_count(mem)

    stable = root_before == root_after
    return stable, {
        "pinned_root_before": "0x" + root_before,
        "pinned_root_after": "0x" + root_after,
        "entries_before": n_before,
        "entries_after": n_after,
        "evicted": max(0, n_before - n_after),
        "advance_seconds": advance_seconds,
    }


def step_spawn_child_and_resolve_bond(
    bob: Bob,
    *,
    child_budget_usdc: float = 1.0,
) -> tuple[dict, dict]:
    child = bob.spawn_child(child_budget_usdc=child_budget_usdc)
    return (
        {"child": child},
        {
            "parent_eoa": bob.address,
            "child_eoa": child.address,
            "child_budget_usdc": child_budget_usdc,
            "constitution_hash_inherited": child.constitution_hash,
        },
    )


def _entry_count(mem: MemoryService) -> int:
    """Return entry count without leaking the private attribute name."""
    for attr in ("entries", "_entries"):
        if hasattr(mem, attr):
            return len(getattr(mem, attr))
    return 0


__all__ = [
    "Orchestrator",
    "TradeIntent",
    "DEFAULT_BOB_RULES",
    "DEFAULT_MARKET_STATE",
    "default_bob_rules",
    "hash_constitution",
    "step_spawn_bob",
    "step_query_alice",
    "step_select_violating_trace",
    "step_attempt_violating_trade",
    "step_decay_check_pinned",
    "step_spawn_child_and_resolve_bond",
]
