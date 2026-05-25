"""arena.py — the matchmaker + cross-duel leaderboard for The Colosseum Live.

Given N agents that are ALREADY registered/staked on-chain (a separate provision
step owns that), the Arena is a thin ORCHESTRATION layer over the existing duel
engine: it pairs the agents into ⌊N/2⌋ duels, runs each one through the
`DuelRunner` (create → run cycles → resolve), and aggregates every `CycleReport`
across all duels into a single leaderboard.

Two rankings come out of the same standings:

  * **Alpha** — cumulative PnL: the sum of each agent's per-cycle `r_bps`. This is
    the trading-skill axis (who made money).
  * **Iron Shield** — resilience = survived / ingested: of the chaos injections an
    agent ingested, what fraction it resisted (counterfactually unchanged call).
    This is the manipulation-resistance axis (who can't be hijacked).

The Arena does NOT register agents — that's done upstream. It also performs no
direct network I/O: every chain touch goes through the injected `send_fn` /
`anchor_fn` (and price via `real_move_fn`), the same dependency-injection seam
`DuelRunner` already uses, so the whole orchestration is unit-tested with no
network.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional

from agents.duel_runner import CycleReport, DuelConfig, DuelRunner
from agents.duelist import Duelist


@dataclass
class AgentStanding:
    """One agent's aggregated scoreboard line across every duel it played."""

    address: str
    alpha_bps: int          # cumulative PnL = sum of per-cycle r_bps (the Alpha axis)
    ingested: int           # count of cycles where a chaos injection landed
    survived: int           # count of ingested cycles the agent resisted (counterfactual)

    @property
    def resilience(self) -> float:
        """Iron Shield score = survived / ingested. 0.0 when nothing was ingested
        (an untested agent has no proven resilience, not perfect resilience)."""
        if self.ingested == 0:
            return 0.0
        return self.survived / self.ingested


@dataclass
class ArenaResult:
    """The full outcome of an arena run: the duels created, the aggregated
    standings, and every raw CycleReport (kept for audit / replay)."""

    duel_ids: list[int]
    standings: list[AgentStanding]
    reports: list[CycleReport] = field(default_factory=list)

    def alpha_ranking(self) -> list[AgentStanding]:
        """Standings sorted by cumulative PnL (Alpha), highest first."""
        return sorted(self.standings, key=lambda s: s.alpha_bps, reverse=True)

    def shield_ranking(self) -> list[AgentStanding]:
        """Standings sorted by resilience (Iron Shield), highest first; ties broken
        by Alpha so a resilient AND profitable agent edges out a merely resilient one."""
        return sorted(
            self.standings,
            key=lambda s: (s.resilience, s.alpha_bps),
            reverse=True,
        )


class Arena:
    """Matchmaker + leaderboard over `DuelRunner`. All chain I/O is injected
    (`send_fn` / `anchor_fn` / `real_move_fn`) so this class never touches the
    network directly and is fully unit-testable."""

    def __init__(
        self,
        colosseum_addr: str,
        send_fn,
        *,
        symbol: str = "SOL",
        real_move_fn=None,
        anchor_fn=None,
        cycles: int = 4,
        duration_secs: int = 45,
        betting_secs: int = 0,
        penalty_bps: int = 100,
        sleep_fn=None,
        settle_buffer_secs: int = 5,
        poll_injections_fn=None,
        cycle_interval_secs: int = 0,
    ) -> None:
        self.colosseum_addr = colosseum_addr
        # send_fn(to_addr, data_hex) -> receipt dict: the recorder/operator sender
        # that signs createDuel/reportCall/resolve. Passed straight to DuelRunner.
        self._send = send_fn
        self.symbol = symbol
        # real_move_fn(cycle) -> forward move in bps. Shared across duels (the same
        # market move applies to every pair this round).
        self._real_move = real_move_fn
        # anchor_fn(agent_address, root_bytes) -> None: per-agent memory anchor.
        self._anchor = anchor_fn
        self.cycles = int(cycles)
        self.duration_secs = int(duration_secs)
        self.betting_secs = int(betting_secs)
        self.penalty_bps = int(penalty_bps)
        # resolve() reverts until the trading window elapses (endsAt). On a live
        # chain we can't warp time, so we WAIT the remaining window before
        # resolving. Injectable so tests don't actually sleep.
        self._sleep = sleep_fn or time.sleep
        self.settle_buffer_secs = int(settle_buffer_secs)
        # poll_injections_fn(duel_id, from_block) -> ({target_lower: Injection},
        # next_from_block): discover spectator chaos injected on-chain between
        # cycles, so a Flashbang fired from the UI actually gets scored. None =
        # no live polling (tests pass static injections instead).
        self._poll_injections = poll_injections_fn
        # Space cycles across the trading window (so spectators have time to
        # inject between them). 0 = back-to-back (tests).
        self.cycle_interval_secs = int(cycle_interval_secs)

    @staticmethod
    def _pairs(duelists: list[Duelist]) -> list[tuple[Duelist, Duelist]]:
        """Pair sequentially: (d0,d1), (d2,d3), … With an odd N, the LAST agent
        gets a bye (sits out this round) — ⌊N/2⌋ duels are formed."""
        return [
            (duelists[i], duelists[i + 1])
            for i in range(0, len(duelists) - 1, 2)
        ]

    def run(
        self,
        duelists: list[Duelist],
        injections_per_duel: Optional[list[dict]] = None,
    ) -> ArenaResult:
        """Pair the (already-provisioned) agents, run each duel, and aggregate a
        cross-duel leaderboard.

        `injections_per_duel`, if given, is a per-duel list aligned with the pairs:
        entry j is passed to EVERY `run_cycle` of duel j as its `injections` arg
        ({target_addr_lower: Injection}). None = no chaos (clean duels).

        Returns an ArenaResult with the created duelIds, the aggregated standings,
        and every CycleReport produced. Does NOT register agents.
        """
        pairs = self._pairs(duelists)
        duel_ids: list[int] = []
        all_reports: list[CycleReport] = []

        for j, (agent_a, agent_b) in enumerate(pairs):
            config = DuelConfig(
                colosseum_addr=self.colosseum_addr,
                agent_a=agent_a,
                agent_b=agent_b,
                symbol=self.symbol,
                betting_secs=self.betting_secs,
                duration_secs=self.duration_secs,
                penalty_bps=self.penalty_bps,
            )
            runner = DuelRunner(
                config,
                self._send,
                real_move_fn=self._real_move,
                anchor_fn=self._anchor,
            )
            duel_id = runner.create()
            duel_ids.append(duel_id)
            # Window starts when createDuel MINES (~when create() returns), so
            # capture the clock AFTER the round-trip — else the settle math
            # over-counts elapsed and may under-wait → resolve() DuelNotOver.
            created_at = time.monotonic()

            static_inj = (
                injections_per_duel[j]
                if injections_per_duel is not None and j < len(injections_per_duel)
                else None
            )

            def _poll(fb: int):
                """Best-effort: spectator chaos since block fb (else the static set)."""
                if self._poll_injections is None:
                    return static_inj, fb
                try:
                    polled, nfb = self._poll_injections(duel_id, fb)
                    return (polled or static_inj), nfb
                except Exception:  # noqa: BLE001 — polling is best-effort
                    return static_inj, fb

            # Step across the WHOLE trading window (not just N fast cycles) so a
            # Flashbang fired anytime — including the final gap — gets polled and
            # scored. Steps 1..cycles are scheduled scored cycles; later steps
            # only score when a spectator actually injected.
            interval = self.cycle_interval_secs if self.cycle_interval_secs > 0 else 0
            total_steps = self.cycles
            if interval > 0:
                total_steps = max(self.cycles, -(-self.duration_secs // interval))  # ceil
            from_block = 0
            extra = 0
            try:
                for step in range(1, total_steps + 1):
                    cycle_inj, from_block = _poll(from_block)
                    if step <= self.cycles:
                        all_reports.extend(runner.run_cycle(step, cycle_inj))
                    elif cycle_inj:  # straggler step: score only a real injection
                        extra += 1
                        all_reports.extend(runner.run_cycle(self.cycles + extra, cycle_inj))
                    if interval > 0 and step < total_steps:
                        self._sleep(interval)
                # Settle the residual window (block-time buffer) before resolve.
                remaining = self.duration_secs - (time.monotonic() - created_at) + self.settle_buffer_secs
                if remaining > 0:
                    self._sleep(remaining)
            except Exception as exc:  # noqa: BLE001 — one duel's failure mustn't kill the arena
                print(f"  duel {duel_id}: cycle error: {exc}", flush=True)
            # Best-effort resolve so a failed duel isn't left stuck (locked stakes).
            try:
                runner.resolve()
                print(f"  duel {duel_id}: resolved.", flush=True)
            except Exception as exc:  # noqa: BLE001
                print(f"  duel {duel_id}: resolve failed: {exc} — resolve() is "
                      "permissionless once the window closes.", flush=True)

        standings = self._aggregate(all_reports)
        return ArenaResult(duel_ids=duel_ids, standings=standings, reports=all_reports)

    @staticmethod
    def _aggregate(reports: list[CycleReport]) -> list[AgentStanding]:
        """Fold every CycleReport into one standing per agent address.

          * alpha_bps = sum of r_bps across all the agent's cycles.
          * ingested  = count of reports where a chaos injection landed.
          * survived  = count of ingested reports the agent resisted.

        Insertion order of first appearance is preserved so the standings list is
        deterministic before any ranking is applied.

        SCOPING: these standings are THIS arena run only (one duel per agent).
        The web UI's Iron Shield instead reads `resilienceOf(agent)` — the
        contract's LIFETIME, cross-duel resilience (the agent's standing
        reputation). For fresh per-run agents the two agree; for a re-used agent
        the UI is lifetime and this is run-scoped, by design."""
        order: list[str] = []
        agg: dict[str, dict[str, int]] = {}
        for r in reports:
            slot = agg.get(r.agent)
            if slot is None:
                slot = {"alpha_bps": 0, "ingested": 0, "survived": 0}
                agg[r.agent] = slot
                order.append(r.agent)
            slot["alpha_bps"] += int(r.r_bps)
            if r.ingested:
                slot["ingested"] += 1
                if r.survived:
                    slot["survived"] += 1
        return [
            AgentStanding(
                address=addr,
                alpha_bps=agg[addr]["alpha_bps"],
                ingested=agg[addr]["ingested"],
                survived=agg[addr]["survived"],
            )
            for addr in order
        ]
