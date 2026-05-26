#!/usr/bin/env python3
"""run_arena.py — Task A7, the arena host launcher (Python orchestrator).

One command that turns N fresh keypairs into a live competition on Arc:

  spawn_keypairs(N)        — mint N agent EVM keys (encrypted keystores)
  provision_agents(...)    — operator funds each, agent self-mints its ERC-8004
                             identity, agent approves + self-registers in the
                             Colosseum (so the AGENT owns its identity + stake)
  assemble(...)            — build ONE shared Embedder, a per-agent MemoryService,
                             and a memory-augmented Duelist per wallet; wire the
                             Arena's chain seams (send/anchor/price)
  arena.run(duelists)      — pair the agents, run a round of duels through the
                             DuelRunner, aggregate the leaderboard
  print rankings + tx ids  — Alpha (PnL) + Iron Shield (resilience) + duelIds

The live run is operator-gated: it broadcasts REAL transactions, spends faucet
USDC for gas + stakes, and the duelists make REAL model calls (never faked). The
assembly wiring (`assemble`) is a pure function with every chain/price/embed seam
injectable, so it is unit-tested offline with no network and no provider key.

SECURITY: agent private keys live only in the in-memory ``AgentWallet`` objects
and are forwarded to in-process signing (``cast_send`` / ``anchor_memory``) — they
are never logged and never placed on argv (same contract as ``scripts.lib.chain``).
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Callable, Optional, Sequence

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from agents.agent_wallet import (  # noqa: E402
    AgentKeyError,
    AgentWallet,
    load_agent_wallets,
    save_identities,
    spawn_keypairs,
)
from agents.arena import Arena  # noqa: E402
from agents.duel_runner import SOL_USD_FEED, SYMBOL_FEEDS, PythScorer  # noqa: E402
from agents.duelist import Duelist  # noqa: E402
from agents.embedder import Embedder  # noqa: E402
from agents.memory_service import MemoryService  # noqa: E402
from agents.provision import provision_agents  # noqa: E402

# Four distinct one-line strategy personas so an arena of N agents is a real
# competition (momentum vs mean-reversion vs contrarian vs breakout) rather than
# N identical bots. Assigned round-robin in assemble().
DEFAULT_PERSONAS = (
    "Momentum: ride the trend — go with the direction of the oracle move.",
    "Mean-reversion: fade extremes — bet the move reverts toward its mean.",
    "Contrarian: lean against the crowd and against panic-driven headlines.",
    "Breakout: trade decisive range breaks; sit out chop, commit on conviction.",
)


def assemble(
    wallets: Sequence[AgentWallet],
    *,
    colosseum: str,
    memory_anchor: str,
    rpc_url: str,
    operator_pk: str,
    symbol: str = "SOL",
    model: Optional[str] = None,
    cycles: int = 4,
    duration_secs: int = 45,
    poll_injections_fn=None,
    cycle_interval_secs: int = 0,
    send_fn: Optional[Callable[[str, str], dict]] = None,
    anchor_fn: Optional[Callable[[str, bytes], object]] = None,
    real_move_fn: Optional[Callable[[int], int]] = None,
    embedder: Optional[Embedder] = None,
    personas: Optional[Sequence[str]] = None,
) -> tuple[Arena, list[Duelist]]:
    """Build the Arena + the memory-augmented Duelists from provisioned wallets.

    Pure assembly — no network is touched here. Every chain/price/embed seam is
    injectable so this is fully unit-testable offline:

      * ``embedder``     — ONE shared Embedder across all agents (or the injected
                           one). The agents share embed weights; they each keep
                           their OWN MemoryService (one per agent).
      * ``send_fn``      — the operator recorder that signs createDuel / reportCall
                           / resolve. Default: in-process operator sign+wait.
      * ``anchor_fn``    — per-agent memory anchor. Default: routes (addr, root) to
                           THAT agent's own private key + ERC-8004 identity_id, so
                           the agent (not the operator) signs its own anchor.
      * ``real_move_fn`` — per-cycle market move in bps. Default: live Pyth scorer
                           for ``symbol`` (shared across every duel this round).
      * ``personas``     — round-robin strategy strings (default: 4 distinct).

    Each Duelist's ``hardened`` flag ALTERNATES (True, False, True, …) so every
    duel pairs a defended agent against a naive one — the single controlled
    variable the resilience benchmark measures.

    Returns ``(arena, duelists)``. The duelists are passed to ``arena.run(...)``.
    """
    wallets = list(wallets)
    personas = list(personas) if personas else list(DEFAULT_PERSONAS)
    if not personas:
        raise ValueError("personas must be non-empty")

    # ONE shared embedder (real MiniLM weights are loaded once, lazily) — the
    # memory store is PER agent, the embedding model is shared.
    shared_embedder = embedder if embedder is not None else Embedder()

    duelists: list[Duelist] = []
    for i, w in enumerate(wallets):
        duelists.append(
            Duelist(
                address=w.address,
                hardened=(i % 2 == 0),  # alternate hardened/naive across the field
                model=model,
                memory=MemoryService(dim=384),  # one per agent
                embedder=shared_embedder,
                persona=personas[i % len(personas)],
            )
        )

    # --- Default chain seams (in-process signing; keys never on argv) --------
    if send_fn is None:
        from scripts.lib.chain import cast_send, wait_for_receipt

        def send_fn(to: str, data: str) -> dict:  # operator recorder
            return wait_for_receipt(
                rpc_url, cast_send(rpc_url=rpc_url, pk=operator_pk, to=to, data=data)
            )

    if anchor_fn is None:
        from scripts.anchor_memory import anchor_memory

        # Map address -> wallet so each agent anchors with its OWN key + identity.
        by_addr = {w.address.lower(): w for w in wallets}

        def anchor_fn(addr: str, root: bytes) -> dict:  # per-agent, agent-owned
            agent = by_addr[addr.lower()]
            return anchor_memory(
                rpc_url=rpc_url,
                pk=agent.private_key,
                anchor_address=memory_anchor,
                root_hex="0x" + root.hex(),
                identity_id=agent.identity_id,
            )

    if real_move_fn is None:
        scorer = PythScorer(SYMBOL_FEEDS.get(symbol.upper(), SOL_USD_FEED))
        real_move_fn = scorer.move_bps

    arena = Arena(
        colosseum,
        send_fn,
        symbol=symbol,
        real_move_fn=real_move_fn,
        anchor_fn=anchor_fn,
        cycles=cycles,
        duration_secs=duration_secs,
        poll_injections_fn=poll_injections_fn,
        cycle_interval_secs=cycle_interval_secs,
    )
    return arena, duelists


def acquire_pool(
    *,
    reuse_keystores: Optional[str],
    agents: int,
    rpc_url: str,
    operator_pk: str,
    colosseum: str,
    fund_usdc: float,
    stake_usdc: float,
) -> list[AgentWallet]:
    """Obtain the agent pool — either REUSE an existing provisioned one or spawn
    a fresh one and provision it on Arc.

    Two mutually exclusive paths:

      * REUSE (``reuse_keystores`` set): decrypt the encrypted keystores in that
        dir back into wallets (each carrying its minted ``identity_id`` from the
        ``identities.json`` sidecar) and return them AS-IS. NO ``spawn_keypairs``,
        NO ``provision_agents`` — the agents are already minted/registered/staked,
        so this spends ZERO USDC/gas and removes the provisioning gap. The keys
        stay in memory; nothing is logged.

      * FRESH (default): ``spawn_keypairs(agents)`` mints N encrypted keystores,
        then ``provision_agents`` funds + self-mints identities + self-registers
        each on Arc. The minted ``identity_id`` map is persisted via
        ``save_identities`` so THIS pool can be reused later with
        ``--reuse-keystores``.

    Returns the list of ready-to-duel ``AgentWallet`` objects (each with a non-
    ``None`` ``identity_id`` on success).
    """
    if reuse_keystores:
        print(f"[1/4] reusing provisioned pool from {reuse_keystores} "
              "(skip spawn + provision) ...")
        wallets = load_agent_wallets(keystore_dir=reuse_keystores)
        if not wallets:
            raise SystemExit(
                f"--reuse-keystores {reuse_keystores}: no *.keystore files found "
                "there. Provision a pool first (run without --reuse-keystores)."
            )
        if len(wallets) < 2:
            raise SystemExit(
                f"--reuse-keystores {reuse_keystores}: need at least 2 agents to "
                f"form a duel, found {len(wallets)}."
            )
        missing = [w.address for w in wallets if w.identity_id is None]
        if missing:
            raise SystemExit(
                f"--reuse-keystores {reuse_keystores}: {len(missing)} keystore(s) "
                "have no ERC-8004 identity_id in the identities.json sidecar — the "
                "pool is not fully provisioned. Re-provision (run without "
                "--reuse-keystores) or supply the sidecar."
            )
        print(f"[2/4] loaded {len(wallets)} pre-provisioned agents (no spend).")
        return wallets

    # Fresh path: spawn N keypairs, then provision them on Arc.
    print(f"[1/4] spawning {agents} agent keypairs ...")
    wallets = spawn_keypairs(agents)

    print("[2/4] provisioning agents on Arc (fund + self-mint identity + register) ...")
    wallets = provision_agents(
        wallets,
        rpc_url=rpc_url,
        operator_pk=operator_pk,
        colosseum=colosseum,
        fund_usdc=fund_usdc,
        stake_usdc=stake_usdc,
    )
    # Persist the minted address->identity_id map so this fresh pool can be
    # REUSED later (--reuse-keystores) without re-spending USDC/gas.
    try:
        path = save_identities(wallets)
        print(f"    saved identities sidecar -> {path} (pool now reusable)")
    except OSError as exc:  # non-fatal: the run can still proceed this round
        print(f"    warning: could not persist identities sidecar: {exc}")
    return wallets


def _format_rankings(result) -> str:
    """Render the Alpha + Iron Shield leaderboards as printable text."""
    lines: list[str] = []
    lines.append("\n===== ALPHA RANKING (cumulative PnL) =====")
    for rank, s in enumerate(result.alpha_ranking(), start=1):
        lines.append(
            f"  #{rank}  {s.address}  alpha={s.alpha_bps:+d} bps  "
            f"resilience={s.resilience:.2f} ({s.survived}/{s.ingested})"
        )
    lines.append("\n===== IRON SHIELD RANKING (resilience) =====")
    for rank, s in enumerate(result.shield_ranking(), start=1):
        lines.append(
            f"  #{rank}  {s.address}  resilience={s.resilience:.2f} "
            f"({s.survived}/{s.ingested})  alpha={s.alpha_bps:+d} bps"
        )
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    """Live launcher. Spawns N agents, provisions them on Arc, assembles the
    memory-augmented duelists, runs an arena round, and prints the leaderboard.

    Operator-gated: requires a funded operator key + provider key + Arc RPC. The
    duelists make REAL model calls — this never fabricates decisions."""
    from scripts.lib.envfile import load_env

    load_env()

    p = argparse.ArgumentParser(
        description="Run a live Arena round (N autonomous memory-augmented agents)."
    )
    p.add_argument("--colosseum", required=True, help="Deployed Colosseum address.")
    p.add_argument(
        "--memory-anchor", required=True, help="Deployed MemoryAnchor address."
    )
    p.add_argument("--agents", type=int, default=4, help="Number of agents (default 4).")
    p.add_argument(
        "--reuse-keystores",
        "--pool",
        dest="reuse_keystores",
        default=None,
        metavar="DIR",
        help="Reuse an ALREADY-provisioned agent pool: load encrypted keystores "
        "from DIR (with their identities.json sidecar) and skip spawn + "
        "provision entirely. The agents must already be minted/registered/staked "
        "from a prior run. Saves USDC/gas and removes the provisioning gap.",
    )
    p.add_argument("--cycles", type=int, default=4, help="Scored cycles per duel.")
    p.add_argument("--duration", type=int, default=45,
                   help="Trading-window seconds per duel (resolve waits it out). "
                        "Use a few minutes if you want to inject chaos from the UI.")
    p.add_argument("--symbol", default="SOL", help="Pyth-scored asset (default SOL).")
    p.add_argument(
        "--stake-usdc", type=float, default=1.0, help="Per-agent Colosseum stake."
    )
    p.add_argument(
        "--fund-usdc",
        type=float,
        default=2.0,
        help="USDC the operator disburses to each agent (must cover gas + stake).",
    )
    p.add_argument(
        "--model", default=None, help="Model slug/id (default: Duelist provider default)."
    )
    p.add_argument(
        "--rpc-url",
        default=os.environ.get("ARENA_RPC_URL") or os.environ.get("RPC"),
        help="Arc RPC (default $ARENA_RPC_URL / $RPC).",
    )
    args = p.parse_args(argv)

    # Provider key: the duelists make REAL model calls — refuse without one,
    # mirroring agents.duel_runner.main.
    if not (os.environ.get("OPENROUTER_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")):
        raise SystemExit(
            "need $OPENROUTER_API_KEY (OpenAI-compatible, routes to Claude/any model) "
            "or $ANTHROPIC_API_KEY — the arena's duelists make REAL model calls; this "
            "launcher never fabricates decisions."
        )

    operator_pk = os.environ.get("DEPLOYER_PK")
    if not operator_pk:
        raise SystemExit(
            "need an operator signer: set $DEPLOYER_PK (the funded Arc operator key). "
            "It is used in-process only — never placed on argv."
        )
    if not args.rpc_url:
        raise SystemExit(
            "need an Arc RPC: pass --rpc-url or set $ARENA_RPC_URL / $RPC."
        )
    # The fresh path needs --agents >= 2 to form a duel; the reuse path takes its
    # count from the loaded pool (validated inside acquire_pool) and ignores --agents.
    if not args.reuse_keystores and args.agents < 2:
        raise SystemExit("need at least 2 agents to form a duel (--agents >= 2).")

    print(f"================ The Arena — LIVE on Arc ================")
    print(f"  pool      : {'reuse ' + args.reuse_keystores if args.reuse_keystores else 'fresh spawn'}")
    print(f"  agents    : {args.agents if not args.reuse_keystores else '(from pool)'}")
    print(f"  symbol    : {args.symbol} (scored on live Pyth)")
    print(f"  colosseum : {args.colosseum}")
    print(f"  anchor    : {args.memory_anchor}")
    print(f"  cycles    : {args.cycles}")
    print(f"========================================================")

    # 1+2. Acquire the agent pool: REUSE an already-provisioned one (skip spawn +
    #      provision, zero spend) or spawn N fresh keypairs and provision them.
    try:
        wallets = acquire_pool(
            reuse_keystores=args.reuse_keystores,
            agents=args.agents,
            rpc_url=args.rpc_url,
            operator_pk=operator_pk,
            colosseum=args.colosseum,
            fund_usdc=args.fund_usdc,
            stake_usdc=args.stake_usdc,
        )
    except AgentKeyError as exc:
        raise SystemExit(f"could not load reused keystore pool: {exc}")
    for w in wallets:
        print(f"    agent {w.address}  identity_id={w.identity_id}")

    # 3. Assemble the arena + memory-augmented duelists (real chain seams).
    # Live injection poll: discover spectator chaos on-chain between cycles, and
    # space cycles across the window so a UI-fired Flashbang lands in a gap.
    from agents.duel_runner import poll_injections as _poll_injections

    def _poll(duel_id: int, from_block: int):
        return _poll_injections(args.rpc_url, args.colosseum, duel_id, from_block, args.symbol)

    cycle_interval = max(1, args.duration // (args.cycles + 1))
    print("[3/4] assembling memory-augmented duelists + arena ...")
    arena, duelists = assemble(
        wallets,
        colosseum=args.colosseum,
        memory_anchor=args.memory_anchor,
        rpc_url=args.rpc_url,
        operator_pk=operator_pk,
        symbol=args.symbol,
        model=args.model,
        cycles=args.cycles,
        duration_secs=args.duration,
        poll_injections_fn=_poll,
        cycle_interval_secs=cycle_interval,
    )

    # 4. Run the round of duels and aggregate the cross-duel leaderboard.
    print(f"[4/4] running the arena: {len(duelists)} agents, {args.cycles} cycles/duel ...")
    result = arena.run(duelists)

    print(_format_rankings(result))
    print(f"\nduel ids: {result.duel_ids}")
    print(
        "\nView the live duels + chaos ledger on the explorer:\n"
        f"    https://testnet.arcscan.app/address/{args.colosseum}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
