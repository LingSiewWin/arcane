"""agent_runner.py — the CONTINUOUS layer of the Agent Arena (sub-project 2).

The registry API (``agents/registry_api.py``) is request/response: it does one
thing per call. The arena's whole point, though, is that it's a *living*
economy — agents keep acting, so the UI's live feed keeps streaming. This
module is that loop.

``AgentRunner`` drives a set of already-registered agents through repeated
cycles. Each cycle, for every agent, it:

  1. Publishes a FRESH advice trace (a real templated reasoning string,
     embedded with the SAME MiniLM the dark pool uses) into the shared
     ``MemoryService`` AND emits a real ``recordAction(id, 0, payload)`` —
     so a new ``AgentAction`` event lands on chain every cycle.
  2. Queries the shared dark pool over real x402 (signed EIP-3009 payment),
     proving the agent both contributes and consumes alpha, and emits a
     ``recordAction(id, 1, ...)`` QUERY_PAID action when configured to.
  3. Periodically (every ``resolve_every`` cycles, if an oracle is wired)
     triggers a real ``PerformanceOracle.resolve`` → BOND_SLASHED/RELEASED.

"Continuous" is realised by ``run_forever(interval_secs)``: an infinite loop
that runs a cycle, then sleeps ``interval_secs``, until a stop flag is set
(SIGINT / SIGTERM / ``stop()``). For tests, ``run_n_cycles(n)`` runs exactly
``n`` bounded cycles against an anvil fork and returns the count of
``AgentAction`` events emitted — so the continuous loop is provable without
running forever.

CLI:
    python -m agents.agent_runner --registry <addr> --agents 1,2,3 --interval 15
    python -m agents.agent_runner --registry <addr> --agents 1 --run-n 3

Graceful shutdown: SIGINT/SIGTERM set an internal ``threading.Event``; the
loop finishes the in-flight cycle's current agent, then exits cleanly without
a traceback.
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from agents.registry_api import (
    AdviceBody,
    RegistryConfig,
    RegistryService,
    ResolveBody,
)

log = logging.getLogger("agent_runner")


# Templated reasoning families — real English so MiniLM produces meaningful
# embeddings (mirrors agents/alice.py make_corpus). NOT placeholder text: each
# is a plausible trading-reasoning trace an arena agent would publish.
_REASONING_TEMPLATES = (
    "Momentum on {sym} flipped positive after the {n}-bar breakout; "
    "raising conviction and sizing into the {sym} trend continuation.",
    "Funding on {sym} perps turned rich while spot stalled; fading the "
    "crowded long and rotating risk out of {sym} into cash.",
    "Volatility on {sym} compressed below the {n}-day band; preparing a "
    "straddle ahead of the expected {sym} expansion move.",
    "Liquidity thinned on the {sym} book around the {n} level; tightening "
    "stops and reducing {sym} exposure until depth returns.",
    "Cross-venue basis on {sym} widened past {n} bps; arbing the spread and "
    "hedging the {sym} delta to stay market-neutral.",
)
_SYMBOLS = ("SOL", "ETH", "BTC", "ARB", "JUP")
# Per-template action direction — the herding signal the 3D clustering groups
# on (correlated actions = same symbol + stance in a window). Index-aligned to
# _REASONING_TEMPLATES above.
_TEMPLATE_STANCES = ("long", "exit", "vol", "reduce", "neutral")


def _make_advice(agent_id: int, cycle: int) -> tuple[str, str, str]:
    """Deterministic-but-varying real (reasoning, symbol, stance) for a cycle.

    Varies by agent and cycle so each published advice is genuinely fresh
    content (a new embedding, a new advice hash, a new on-chain action) —
    never the same string twice. The symbol + stance are returned so they go
    on-chain in the structured trace payload (and drive clustering).
    """
    idx = (agent_id + cycle) % len(_REASONING_TEMPLATES)
    tmpl = _REASONING_TEMPLATES[idx]
    sym = _SYMBOLS[(agent_id * 3 + cycle) % len(_SYMBOLS)]
    n = 8 + ((agent_id * 7 + cycle * 5) % 40)
    return tmpl.format(sym=sym, n=n), sym, _TEMPLATE_STANCES[idx]


def _make_advice_trace(agent_id: int, cycle: int) -> str:
    """Back-compat shim — the reasoning string alone (see _make_advice)."""
    return _make_advice(agent_id, cycle)[0]


@dataclass
class RunnerConfig:
    agent_ids: list[int]
    # How often to fire a resolve (0 disables; needs an oracle in RegistryConfig).
    resolve_every: int = 0
    # Map of agent_id -> on-chain advice-owner address (for resolve).
    agent_addrs: dict[int, str] = field(default_factory=dict)
    # Whether to record a QUERY_PAID action each cycle (independent of the
    # x402 query itself, which always runs when the dark pool is mounted).
    record_query_actions: bool = True


@dataclass
class CycleResult:
    cycle: int
    advice_tx: list[str] = field(default_factory=list)
    query_tx: list[str] = field(default_factory=list)
    resolve_tx: list[str] = field(default_factory=list)
    action_events: int = 0
    errors: list[str] = field(default_factory=list)


class AgentRunner:
    """Drives registered agents through continuous arena cycles.

    Stateless w.r.t. the chain except for the ``RegistryService`` it holds;
    every action it takes is a real on-chain ``recordAction`` (or resolve).
    """

    def __init__(self, service: RegistryService, config: RunnerConfig) -> None:
        self.service = service
        self.config = config
        self._stop = threading.Event()
        self._cycle = 0

    # ---- lifecycle -----------------------------------------------------

    def stop(self) -> None:
        """Signal the loop to halt after the current step. Idempotent."""
        self._stop.set()

    @property
    def stopped(self) -> bool:
        return self._stop.is_set()

    def install_signal_handlers(self) -> None:
        """Wire SIGINT/SIGTERM to a graceful stop. Main-thread only."""
        def _handler(signum, _frame):  # noqa: ANN001
            log.info("received signal %s — stopping after current step", signum)
            self.stop()

        signal.signal(signal.SIGINT, _handler)
        signal.signal(signal.SIGTERM, _handler)

    # ---- one cycle -----------------------------------------------------

    def run_cycle(self) -> CycleResult:
        """Run one arena cycle across all agents. Returns a CycleResult.

        Each agent: publish fresh advice (always → 1 AgentAction), optionally
        an x402 query + QUERY_PAID action, and a periodic resolve. Per-agent
        errors are captured (so one agent's failure doesn't halt the cycle)
        but real on-chain failures still surface in ``result.errors``.
        """
        self._cycle += 1
        result = CycleResult(cycle=self._cycle)

        for agent_id in self.config.agent_ids:
            if self._stop.is_set():
                break

            # 1. Publish fresh advice → real recordAction(kind=0). The symbol +
            #    stance go on-chain in the structured trace payload so the UI
            #    can render the full invocation trace and cluster on herding.
            try:
                trace, symbol, stance = _make_advice(agent_id, self._cycle)
                advice = self.service.publish_advice(
                    agent_id,
                    AdviceBody(trace=trace, kind="working", symbol=symbol, stance=stance),
                )
                result.advice_tx.append(advice["tx_hash"])
                if advice.get("event") is not None:
                    result.action_events += 1
            except Exception as exc:  # noqa: BLE001
                result.errors.append(f"agent {agent_id} advice: {exc}")
                # Advice is the heartbeat; if it fails we skip the rest for
                # this agent but keep the loop alive for the others.
                continue

            if self._stop.is_set():
                break

            # 2. Query the shared dark pool over real x402, then optionally
            #    record a QUERY_PAID action.
            try:
                self._query_dark_pool(agent_id, trace)
                if self.config.record_query_actions:
                    q = self.service.record_query_paid(agent_id)
                    result.query_tx.append(q["tx_hash"])
                    if q.get("event") is not None:
                        result.action_events += 1
            except Exception as exc:  # noqa: BLE001
                result.errors.append(f"agent {agent_id} query: {exc}")

            if self._stop.is_set():
                break

            # 3. Periodic resolve → BOND_SLASHED / BOND_RELEASED.
            if (
                self.config.resolve_every > 0
                and self._cycle % self.config.resolve_every == 0
                and self.service.config.performance_oracle
            ):
                try:
                    agent_addr = self.config.agent_addrs.get(agent_id)
                    res = self.service.resolve_agent(
                        agent_id, ResolveBody(agent_addr=agent_addr)
                    )
                    ra = res.get("recordAction") or {}
                    if ra.get("tx_hash"):
                        result.resolve_tx.append(ra["tx_hash"])
                    if ra.get("event") is not None:
                        result.action_events += 1
                except Exception as exc:  # noqa: BLE001
                    result.errors.append(f"agent {agent_id} resolve: {exc}")

        return result

    def _query_dark_pool(self, agent_id: int, trace: str) -> None:
        """Run a real x402-paid query against the composed dark pool.

        Only runs when a dark pool is mounted AND a query signer is available
        (``ARENA_QUERY_PK``). Without a signer we skip the paid leg — the
        advice heartbeat (the live-feed source) still fires, so the runner
        keeps emitting actions. The query embeds the trace with the same
        embedder the advice path uses, so it actually retrieves relevant alpha.
        """
        dp = self.service.dark_pool
        if dp is None:
            return
        query_pk = os.environ.get("ARENA_QUERY_PK")
        if not query_pk:
            return
        from eth_account import Account
        from fastapi.testclient import TestClient

        from agents.x402_client import x402_query

        signer = Account.from_key(query_pk)
        vec = self.service._embedder.embed(trace)
        # In-process transport against the mounted dark pool app.
        transport = TestClient(dp.app)
        x402_query(
            url="/query",
            query_vec=vec,
            k=5,
            signer=signer,
            chain_id=self.service.config.chain_id,
            asset_address=self.service.config.usdc_address,
            expected_price_usdc=self.service.config.price_per_query_usdc,
            expected_recipient=dp.payment_recipient,
            transport=transport,
        )

    # ---- continuous loops ----------------------------------------------

    def run_n_cycles(self, n: int, *, interval_secs: float = 0.0) -> list[CycleResult]:
        """Run exactly ``n`` bounded cycles (the test-friendly entry point).

        Returns the per-cycle results. Stops early if ``stop()`` is called.
        ``interval_secs`` defaults to 0 so tests run fast; production uses
        ``run_forever``.
        """
        results: list[CycleResult] = []
        for _ in range(n):
            if self._stop.is_set():
                break
            results.append(self.run_cycle())
            if interval_secs > 0 and not self._stop.is_set():
                # Interruptible sleep — stop() wakes us immediately.
                self._stop.wait(timeout=interval_secs)
        return results

    def run_forever(self, interval_secs: float = 15.0) -> list[CycleResult]:
        """Loop cycles forever (until stop()/SIGINT). The production entry point.

        Each iteration: run a cycle, log a one-line summary, sleep
        ``interval_secs`` (interruptibly). Returns all cycle results when
        stopped — useful for a final summary.
        """
        results: list[CycleResult] = []
        log.info(
            "agent_runner starting: agents=%s interval=%ss resolve_every=%s",
            self.config.agent_ids,
            interval_secs,
            self.config.resolve_every,
        )
        while not self._stop.is_set():
            res = self.run_cycle()
            results.append(res)
            log.info(
                "cycle %d: %d AgentAction events (advice=%d query=%d resolve=%d) errors=%d",
                res.cycle,
                res.action_events,
                len(res.advice_tx),
                len(res.query_tx),
                len(res.resolve_tx),
                len(res.errors),
            )
            for err in res.errors:
                log.warning("  %s", err)
            if not self._stop.is_set():
                self._stop.wait(timeout=interval_secs)
        log.info("agent_runner stopped after %d cycles", len(results))
        return results


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_service(args: argparse.Namespace) -> RegistryService:
    rpc_url = args.rpc_url or os.environ.get("ARENA_RPC_URL", "").strip()
    if not rpc_url:
        raise SystemExit("REFUSING: need --rpc-url or $ARENA_RPC_URL.")
    config = RegistryConfig(
        rpc_url=rpc_url,
        registry_addr=args.registry,
        deployer_pk=os.environ.get("DEPLOYER_PK") or None,
        deployer_account=args.account or os.environ.get("DEPLOYER_ACCOUNT") or None,
        chain_id=int(args.chain_id),
        payment_recipient=os.environ.get("DARKPOOL_RECIPIENT") or None,
        usdc_address=os.environ.get(
            "DARKPOOL_USDC_ADDRESS", "0x3600000000000000000000000000000000000000"
        ),
        price_per_query_usdc=os.environ.get("DARKPOOL_PRICE_USDC", "0.001"),
        performance_oracle=args.oracle or os.environ.get("ARENA_PERFORMANCE_ORACLE") or None,
        pyth_addr=os.environ.get("ARENA_PYTH_ADDR") or None,
    )
    return RegistryService(config)


def _parse_agent_ids(raw: str) -> list[int]:
    return [int(x) for x in raw.replace(" ", "").split(",") if x]


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(
        description=(
            "Continuous Agent Arena runner: registered agents publish advice, "
            "query the dark pool, and resolve on a cadence — emitting real "
            "on-chain AgentAction events for the UI's live feed."
        )
    )
    p.add_argument("--registry", required=True, help="AgentRegistry address.")
    p.add_argument("--agents", required=True, help="Comma-separated agent ids, e.g. 1,2,3.")
    p.add_argument("--rpc-url", default=None, help="Chain RPC (or $ARENA_RPC_URL).")
    p.add_argument("--chain-id", default="5042002")
    p.add_argument("--interval", type=float, default=15.0, help="Seconds between cycles.")
    p.add_argument(
        "--run-n",
        type=int,
        default=0,
        help="Run exactly N cycles then exit (bounded mode). 0 => run forever.",
    )
    p.add_argument("--oracle", default=None, help="PerformanceOracle address (for resolve).")
    p.add_argument(
        "--resolve-every",
        type=int,
        default=0,
        help="Trigger a resolve every N cycles (needs --oracle). 0 disables.",
    )
    p.add_argument("--account", default=None, help="Foundry keystore name (preferred signer).")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s"
    )

    service = _build_service(args)
    runner = AgentRunner(
        service,
        RunnerConfig(
            agent_ids=_parse_agent_ids(args.agents),
            resolve_every=args.resolve_every,
        ),
    )
    runner.install_signal_handlers()

    if args.run_n and args.run_n > 0:
        results = runner.run_n_cycles(args.run_n, interval_secs=args.interval)
        total = sum(r.action_events for r in results)
        print(f"ran {len(results)} cycles; {total} AgentAction events emitted")
        for r in results:
            print(
                f"  cycle {r.cycle}: {r.action_events} events "
                f"(advice={len(r.advice_tx)} query={len(r.query_tx)} "
                f"resolve={len(r.resolve_tx)}) errors={len(r.errors)}"
            )
            for err in r.errors:
                print(f"    ERR {err}")
        return 0

    runner.run_forever(interval_secs=args.interval)
    return 0


if __name__ == "__main__":
    sys.exit(main())
