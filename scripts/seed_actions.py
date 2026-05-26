#!/usr/bin/env python3
"""seed_actions — drive a live stream of REAL AgentAction events into a deployed
``AgentRegistry`` so the web "Living economy" colony shows MULTIPLE clusters and
the "Live activity" feed BURSTS with chatter.

WHY THIS EXISTS
---------------
The web panels (web/apps/web/src/lib/arena.ts: useAgents + useLiveFeed) read the
registry's ``AgentAction`` events. The colony (arena-plane.tsx) clusters each
agent by the (symbol, stance) of its *latest* action — clusterKey = ``symbol·stance``.
The live feed streams *every* AgentAction. So:

  * MULTIPLE clusters  ⇐ the agents' latest actions span several distinct
    (symbol, stance) buckets at the same time.
  * a BURSTING feed     ⇐ actions land frequently (every few seconds).

HOW
---
For each seeded agent we call the REAL on-chain
``AgentRegistry.recordAction(agentId, 0, abi.encode(reasoning, symbol, stance,
adviceHash))``. kind 0 = ADVICE_PUBLISHED. adviceHash = keccak(reasoning) — the
same dark-pool memory commitment ``agents/registry_api.py`` uses.

ACCESS CONTROL (read from contracts/src/AgentRegistry.sol): ``recordAction``
reverts ``NotAgentOperator()`` unless ``msg.sender == agent.operator``. The
arena seeder registered all agents under the operator key (DEPLOYER_PK), so the
operator key is the authorised caller. We sign in-process with ``cast_send``
(key never in argv).

CLUSTER STRATEGY
----------------
We give each agent a DISTINCT home bucket from the {SOL,ETH,BTC} × {long,short}
grid (6 buckets, 6 agents → 6 simultaneous clusters by default). Each cycle every
agent re-publishes its current bucket (keeps ≥N clusters alive + feeds the feed),
and on a slow rotation the agents drift to neighbouring buckets so the colony
visibly migrates — all real, all on-chain.

NOTE on stance vocabulary: ``agents/registry_api.encode_advice_payload`` coerces
unknown stances to "neutral", which would collapse short/long into one bucket.
The web only needs the raw (symbol, stance) strings to cluster, so we abi-encode
the payload directly here with the exact stances requested ("long"/"short").

USAGE
-----
    agents/.venv/bin/python -m scripts.seed_actions \
        --rpc-url https://rpc.testnet.arc.network \
        --registry 0xedC0F5FEEa64F12BfB01e2A1C3a00C8e93533c97 \
        --agents 1,2,3,4,5,6 \
        --interval 4 --duration 300

Omit ``--duration`` (or pass 0) to run forever (supervised demo loop).
Agents default to the ids in deployments/arena.json if ``--agents`` is omitted.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from eth_abi import encode as abi_encode  # noqa: E402
from eth_utils import keccak  # noqa: E402

from scripts.lib.chain import cast_address_from_pk, cast_send, rpc_call  # noqa: E402
from scripts.lib.envfile import load_env  # noqa: E402

ARENA_JSON = REPO_ROOT / "deployments" / "arena.json"
ARC_EXPLORER = "https://testnet.arcscan.app/tx/"

# AgentAction kind 0 = ADVICE_PUBLISHED (mirrors AgentRegistry.sol).
KIND_ADVICE = 0

# The (symbol, stance) grid the colony clusters on. 6 buckets → up to 6 clusters.
SYMBOLS = ("SOL", "ETH", "BTC")
STANCES = ("long", "short")
BUCKETS = [(s, st) for s in SYMBOLS for st in STANCES]  # 6 buckets

_ADVICE_PAYLOAD_TYPES = ["string", "string", "string", "bytes32"]

# A little flavour so each action carries distinct reasoning text (→ distinct
# adviceHash) — makes the feed read like real chatter, not a repeated string.
_REASON_TEMPLATES = {
    "long": [
        "{sym} reclaiming structure; momentum + funding favour a long here.",
        "Accumulation on {sym}; bids stacking, I'm leaning long.",
        "{sym} basis turning positive — opening a long, tight invalidation.",
        "Volume divergence on {sym} says continuation up; long.",
    ],
    "short": [
        "{sym} rejecting resistance; distribution into strength, going short.",
        "Funding overheated on {sym}; fading the move, short.",
        "{sym} losing the range low — short with momentum.",
        "Liquidity above {sym} swept; expecting mean reversion, short.",
    ],
}


def _eth_call(rpc_url: str, to: str, data: str) -> str:
    return rpc_call(rpc_url, "eth_call", [{"to": to, "data": data}, "latest"])


def agent_operator(rpc_url: str, registry: str, agent_id: int) -> Optional[str]:
    """Return the on-chain operator of ``agent_id`` (lowercase) or None.

    Reads ``getAgent(uint256)`` and pulls the operator field (5th member of the
    Agent tuple) so we can fail loud if the key we hold can't call recordAction.
    """
    sel = keccak(b"getAgent(uint256)")[:4]
    data = "0x" + (sel + abi_encode(["uint256"], [int(agent_id)])).hex()
    try:
        ret = _eth_call(rpc_url, registry, data)
    except Exception:
        return None
    from eth_abi import decode as abi_decode

    raw = bytes.fromhex(ret.removeprefix("0x"))
    tup = "(uint256,bytes32,address,string,address,uint64,bool)"
    try:
        decoded = abi_decode([tup], raw)[0]
    except Exception:
        return None
    operator = decoded[4]
    return "0x" + bytes(operator)[-20:].hex() if isinstance(operator, (bytes, bytearray)) else str(operator).lower()


def encode_advice_payload(reasoning: str, symbol: str, stance: str) -> bytes:
    """abi.encode(string reasoning, string symbol, string stance, bytes32 adviceHash).

    adviceHash = keccak(reasoning) — the dark-pool memory commitment, matching
    the convention in agents/registry_api.py. We DO NOT coerce the stance, so the
    requested long/short land verbatim and cluster distinctly in the UI.
    """
    advice_hash = keccak(reasoning.encode())
    return abi_encode(
        _ADVICE_PAYLOAD_TYPES, [reasoning, symbol.upper(), stance, advice_hash]
    )


def encode_record_action(agent_id: int, kind: int, payload: bytes) -> str:
    sel = keccak(b"recordAction(uint256,uint8,bytes)")[:4]
    body = abi_encode(["uint256", "uint8", "bytes"], [int(agent_id), int(kind), bytes(payload)])
    return "0x" + (sel + body).hex()


def _load_default_agent_ids() -> list[int]:
    if ARENA_JSON.is_file():
        try:
            data = json.loads(ARENA_JSON.read_text())
            ids = [int(a["agent_id"]) for a in data.get("agents", [])]
            if ids:
                return ids
        except Exception:
            pass
    return []


def emit_action(
    *,
    rpc_url: str,
    pk: str,
    registry: str,
    agent_id: int,
    symbol: str,
    stance: str,
    rng: random.Random,
) -> str:
    """Emit ONE real recordAction(agent_id, 0, payload). Returns the tx hash."""
    reasoning = rng.choice(_REASON_TEMPLATES[stance]).format(sym=symbol)
    payload = encode_advice_payload(reasoning, symbol, stance)
    calldata = encode_record_action(agent_id, KIND_ADVICE, payload)
    return cast_send(
        rpc_url=rpc_url,
        pk=pk,
        to=registry,
        data=calldata,
        gas_limit=300_000,
    )


def run(
    *,
    rpc_url: str,
    pk: str,
    registry: str,
    agent_ids: list[int],
    interval: float = 4.0,
    duration: float = 300.0,
    rotate_every: int = 8,
    seed: int = 0,
) -> dict:
    """Drive the live action loop.

    Each agent owns a distinct starting bucket (so clusters are spread from the
    first cycle). Every ``rotate_every`` cycles the buckets rotate by one, so the
    colony visibly migrates without ever collapsing to a single cluster.

    ``duration <= 0`` → run until interrupted (supervised demo).
    """
    operator = cast_address_from_pk(pk)
    rng = random.Random(seed or None)

    # Assign each agent a distinct home bucket (round-robin over the 6 buckets).
    home = {aid: BUCKETS[i % len(BUCKETS)] for i, aid in enumerate(agent_ids)}

    start = time.time()
    cycle = 0
    emitted = 0
    bucket_hits: dict[str, int] = {}
    print(
        f"[seed_actions] operator={operator} registry={registry} "
        f"agents={agent_ids} interval={interval}s "
        f"duration={'forever' if duration <= 0 else f'{duration:.0f}s'}",
        flush=True,
    )

    try:
        while True:
            offset = (cycle // max(1, rotate_every))
            for idx, aid in enumerate(agent_ids):
                bsym, bstance = BUCKETS[(idx + offset) % len(BUCKETS)]
                try:
                    tx = emit_action(
                        rpc_url=rpc_url,
                        pk=pk,
                        registry=registry,
                        agent_id=aid,
                        symbol=bsym,
                        stance=bstance,
                        rng=rng,
                    )
                    emitted += 1
                    key = f"{bsym}·{bstance}"
                    bucket_hits[key] = bucket_hits.get(key, 0) + 1
                    print(
                        f"[c{cycle}] agent {aid} → {key}  tx {tx}  {ARC_EXPLORER}{tx}",
                        flush=True,
                    )
                except Exception as exc:  # loud, but keep the demo alive
                    print(f"[c{cycle}] agent {aid} recordAction FAILED: {exc}", file=sys.stderr, flush=True)
                # small stagger so the feed bursts steadily, not all at once
                time.sleep(max(0.0, interval / max(1, len(agent_ids))))
            cycle += 1
            if duration > 0 and (time.time() - start) >= duration:
                break
    except KeyboardInterrupt:
        print("\n[seed_actions] interrupted — stopping cleanly.", flush=True)

    summary = {
        "registry": registry,
        "operator": operator,
        "agents": agent_ids,
        "cycles": cycle,
        "actions_emitted": emitted,
        "clusters_touched": bucket_hits,
        "distinct_clusters": len(bucket_hits),
    }
    print("[seed_actions] SUMMARY:", json.dumps(summary, indent=2), flush=True)
    return summary


def main(argv: Optional[list[str]] = None) -> int:
    load_env()  # pull DEPLOYER_PK etc. from repo-root .env if present
    p = argparse.ArgumentParser(description="Stream real AgentAction advice events for the live colony + feed.")
    p.add_argument("--rpc-url", default=os.environ.get("RPC") or "https://rpc.testnet.arc.network")
    p.add_argument("--registry", required=True, help="AgentRegistry address.")
    p.add_argument("--agents", default=None, help="Comma-separated agent ids (default: from deployments/arena.json).")
    p.add_argument("--interval", type=float, default=4.0, help="Seconds per full cycle across all agents.")
    p.add_argument("--duration", type=float, default=300.0, help="Total seconds to run (<=0 = forever).")
    p.add_argument("--rotate-every", type=int, default=8, help="Rotate cluster assignment every N cycles.")
    p.add_argument("--pk", default=None, help="Operator key (else $DEPLOYER_PK).")
    args = p.parse_args(argv)

    pk = args.pk or os.environ.get("DEPLOYER_PK", "").strip()
    if not pk:
        print("REFUSING: need --pk or $DEPLOYER_PK (the agents' operator key).", file=sys.stderr)
        return 3

    rpc_url = (args.rpc_url or "").strip()
    if not rpc_url:
        print("REFUSING: need --rpc-url or $RPC.", file=sys.stderr)
        return 3

    if args.agents:
        agent_ids = [int(x) for x in args.agents.split(",") if x.strip()]
    else:
        agent_ids = _load_default_agent_ids()
    if not agent_ids:
        print("REFUSING: no agent ids (pass --agents or seed deployments/arena.json first).", file=sys.stderr)
        return 3

    # Verify the operator key can actually call recordAction on these agents.
    operator = cast_address_from_pk(pk).lower()
    bad: list[int] = []
    for aid in agent_ids:
        op = agent_operator(rpc_url, args.registry, aid)
        if op is not None and op.lower() != operator:
            bad.append(aid)
    if bad:
        print(
            f"REFUSING: operator {operator} is NOT the registered operator for agents {bad}; "
            f"recordAction would revert NotAgentOperator(). Use the agents' operator key.",
            file=sys.stderr,
        )
        return 4

    run(
        rpc_url=rpc_url,
        pk=pk,
        registry=args.registry,
        agent_ids=agent_ids,
        interval=args.interval,
        duration=args.duration,
        rotate_every=args.rotate_every,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
