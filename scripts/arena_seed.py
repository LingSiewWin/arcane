#!/usr/bin/env python3
"""arena_seed — programmatic seeder for the AgoraHack "Agent Arena".

Registers N seed agents (default 3) in a freshly-deployed ``AgentRegistry`` so
the arena economy is populated the instant it comes up. For EACH agent, in the
exact order ``AgentRegistry.register`` requires, this:

  1. Mints a REAL ERC-8004 identity owned by the agent's signer, via the
     canonical ``register(string,(string,bytes)[])`` on the Arc identity
     registry (0x8004…). Reuses ``scripts.demo_e2e.register_identity`` so the
     mint path is the SAME real one the demo uses — no mock identity.
  2. Posts a REAL bond (default 1 USDC) in the shared ``BondVault``
     (``approve`` USDC then ``post(amount)``) via ``scripts.demo_e2e.post_bond``
     so the agent has skin in the game (``AgentRegistry`` reverts ``NoBond``
     otherwise).
  3. Defines + hashes the agent's constitution on-chain via
     ``scripts.demo_e2e.define_constitution`` (real
     ``ConstitutionRegistry.defineConstitution``), producing the
     ``constitutionHash`` the agent commits to.
  4. Calls ``AgentRegistry.register(identityId, constitutionHash, bondVault,
     darkPoolUrl)`` and parses the ``AgentRegistered`` event for the minted
     ``agentId``.

OPERATOR MODEL (disclosed, not faked): for the demo all N agents share ONE
signer — the deployer/operator key. They are therefore *operated by one key*
but are *distinct on-chain entities*: each holds its OWN ERC-8004 identity NFT
(minted in step 1) and its OWN AgentRegistry agentId (one agent per identity,
enforced on-chain by ``IdentityAlreadyRegistered``). A single operator
controlling several distinct agents is exactly the multi-agent arena topology.

Because the shared operator's ``BondVault.balanceOf`` is checked by
``register``, the per-agent ``post`` calls accumulate into that one balance —
so the bond requirement is satisfied for every agent (each adds its own 1 USDC
of real stake; the running balance is recorded per agent in the summary).

Output: a JSON summary printed to stdout AND written to ``deployments/arena.json``:

    {
      "registry_addr": "0x…",
      "operator": "0x…",
      "chain_id": 5042002,
      "agents": [
        {"agent_id": 1, "identity_id": …, "identity_tx": "0x…",
         "approve_tx": "0x…", "post_tx": "0x…", "define_tx": "0x…",
         "constitution_hash": "0x…", "register_tx": "0x…",
         "dark_pool_url": "…", "bond_amount_units": 1000000}, …
      ]
    }

This module is import-safe (no side effects at import time). It is invoked by
``scripts/arena_live.sh`` after the contracts are deployed, or directly:

    python -m scripts.arena_seed \
        --rpc-url "$RPC" --account arc-deployer \
        --registry 0x… --bond-vault 0x… --constitution-registry 0x… \
        --n 3 --bond-usdc 1.0
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Callable, Optional

# Make 'agents' and 'scripts' importable when invoked as a script.
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from eth_abi import encode as abi_encode  # noqa: E402
from eth_utils import keccak, to_canonical_address  # noqa: E402

from scripts.demo_e2e import (  # noqa: E402
    define_constitution,
    post_bond,
    register_identity,
    USDC_ADDR,
)
from scripts.lib.chain import (  # noqa: E402
    cast_address_from_pk,
    cast_send,
    chain_id,
    wait_for_receipt,
)

DEPLOYMENTS_DIR = REPO_ROOT / "deployments"
ARENA_JSON = DEPLOYMENTS_DIR / "arena.json"
ARC_EXPLORER = "https://testnet.arcscan.app/tx/"

# Arc testnet canonical ERC-8004 identity registry (ERC-721).
ARC_IDENTITY_REGISTRY_DEFAULT = "0x8004A818BFB912233c491871b3d84c89A494BD9e"

# topic0 for AgentRegistered(uint256,uint256,address,bytes32).
_AGENT_REGISTERED_TOPIC = "0x" + keccak(
    b"AgentRegistered(uint256,uint256,address,bytes32)"
).hex()


def _usdc_to_units(amount_usdc: float) -> int:
    """USDC has 6 decimals on Arc."""
    return int(round(float(amount_usdc) * 1_000_000))


def seed_constitution_rules(agent_index: int) -> list[dict]:
    """A REAL, distinct constitution per seed agent.

    Each agent commits to a genuinely different rule set so their on-chain
    constitution hashes differ — three distinct agents, three distinct
    rule books. The rules use the same kinds + param shapes
    ``agents.bob.rules_to_solidity`` / ``scripts.demo_e2e.define_constitution``
    already understand (so ``defineConstitution`` stores them, not garbage).

    The MAX_TRADE_SIZE cap scales with the agent index so each agent's
    constitution genuinely differs (different keccak), modelling a roster of
    agents with different risk appetites.
    """
    max_usdc = 1.0 + float(agent_index)  # 1.0, 2.0, 3.0, …
    return [
        {
            "rule_id": f"MAX_TRADE_{agent_index}",
            "kind": "MAX_TRADE_SIZE",
            "max_usdc": max_usdc,
        },
        {
            "rule_id": "VENUE_BLACKLIST_DEAD",
            "kind": "VENUE_BLACKLIST",
            "venues": ["0x000000000000000000000000000000000000dEaD"],
        },
    ]


def _register_agent_onchain(
    *,
    rpc_url: str,
    pk: str,
    registry_addr: str,
    identity_id: int,
    constitution_hash: str,
    bond_vault: str,
    dark_pool_url: str,
) -> dict:
    """Call ``AgentRegistry.register(uint256,bytes32,address,string)`` and
    return ``{"agent_id": int, "register_tx": "0x…"}``.

    ABI-encoded in Python (the registry API uses the same shape); the key is
    signed in-process by ``cast_send`` — never argv, never logged.
    """
    ch_bytes = bytes.fromhex(constitution_hash.removeprefix("0x"))
    if len(ch_bytes) != 32:
        raise ValueError("constitution_hash must be 32 bytes")
    sel = keccak(b"register(uint256,bytes32,address,string)")[:4]
    body = abi_encode(
        ["uint256", "bytes32", "address", "string"],
        [
            int(identity_id),
            ch_bytes,
            to_canonical_address(bond_vault),
            dark_pool_url,
        ],
    )
    calldata = "0x" + (sel + body).hex()

    register_tx = cast_send(
        rpc_url=rpc_url,
        pk=pk,
        to=registry_addr,
        data=calldata,
        gas_limit=500_000,
    )
    receipt = wait_for_receipt(rpc_url, register_tx, timeout=90)
    if int(receipt.get("status", "0x0"), 16) != 1:
        raise RuntimeError(
            f"AgentRegistry.register reverted (tx {register_tx}, status "
            f"{receipt.get('status')}); agent {identity_id} was not registered"
        )

    agent_id: Optional[int] = None
    for lg in receipt.get("logs", []) or []:
        if (lg.get("address") or "").lower() != registry_addr.lower():
            continue
        topics = lg.get("topics") or []
        if not topics or topics[0].lower() != _AGENT_REGISTERED_TOPIC.lower():
            continue
        # AgentRegistered(agentId indexed, identityId indexed, operator indexed, hash)
        if len(topics) >= 2:
            agent_id = int(topics[1], 16)
            break
    if agent_id is None:
        raise RuntimeError(
            f"register tx {register_tx} succeeded but no AgentRegistered event "
            f"was found on {registry_addr}; cannot determine the agentId"
        )
    return {"agent_id": agent_id, "register_tx": register_tx}


def seed_arena(
    *,
    rpc_url: str,
    pk: str,
    registry_addr: str,
    bond_vault: str,
    constitution_registry: str,
    n: int = 3,
    bond_usdc: float = 1.0,
    usdc_addr: str = USDC_ADDR,
    identity_registry: str = ARC_IDENTITY_REGISTRY_DEFAULT,
    dark_pool_url_base: str = "http://localhost:8000/pool",
    write_path: Optional[Path] = ARENA_JSON,
    identity_minter: Optional[Callable[..., dict]] = None,
    bond_poster: Optional[Callable[..., dict]] = None,
) -> dict:
    """Seed ``n`` agents into ``registry_addr``. Returns the JSON summary dict.

    Every transaction is real (mint identity → post bond → define constitution
    → AgentRegistry.register) and signed in-process by ``cast_send``. Raises on
    the first hard failure so a half-seeded arena is loud, not silent.

    ``identity_minter`` / ``bond_poster`` default to the REAL Arc helpers
    (``scripts.demo_e2e.register_identity`` against the canonical 0x8004
    ERC-8004 registry, and ``scripts.demo_e2e.post_bond`` against the real
    USDC). They are injection points ONLY so the hermetic anvil-fork pytest can
    drive the same register→agentCount path against mock identity/USDC
    contracts without forking Arc. The live launcher never overrides them.
    """
    operator = cast_address_from_pk(pk)
    bond_units = _usdc_to_units(bond_usdc)
    _mint = identity_minter or (
        lambda: register_identity(
            rpc_url=rpc_url, pk=pk, registry_addr=identity_registry
        )
    )
    _post = bond_poster or (
        lambda: post_bond(
            rpc_url=rpc_url,
            pk=pk,
            vault_addr=bond_vault,
            usdc_addr=usdc_addr,
            amount_units=bond_units,
        )
    )
    try:
        cid = chain_id(rpc_url)
    except Exception:
        cid = 0

    agents: list[dict[str, Any]] = []
    for i in range(n):
        # (1) Mint a fresh ERC-8004 identity OWNED by the operator. Real
        #     register(string,(string,bytes)[]) — distinct identity per agent.
        ident = _mint()
        identity_id = ident["identity_id"]

        # (2) Post a real bond (default 1 USDC) so register's NoBond guard
        #     passes. Per-agent posts accumulate into the operator's balance.
        bond = _post()

        # (3) Define + hash this agent's (distinct) constitution on chain.
        rules = seed_constitution_rules(i)
        constitution_hash, define_tx = define_constitution(
            rpc_url=rpc_url,
            pk=pk,
            registry_addr=constitution_registry,
            rules_dicts=rules,
        )

        # (4) Register the agent in AgentRegistry.
        dark_pool_url = f"{dark_pool_url_base}?agent={i + 1}"
        reg = _register_agent_onchain(
            rpc_url=rpc_url,
            pk=pk,
            registry_addr=registry_addr,
            identity_id=identity_id,
            constitution_hash=constitution_hash,
            bond_vault=bond_vault,
            dark_pool_url=dark_pool_url,
        )

        agents.append(
            {
                "agent_id": reg["agent_id"],
                "identity_id": identity_id,
                "identity_tx": ident.get("register_tx"),
                "approve_tx": bond.get("approve_tx"),
                "post_tx": bond.get("post_tx"),
                "define_tx": define_tx,
                "constitution_hash": constitution_hash,
                "register_tx": reg["register_tx"],
                "dark_pool_url": dark_pool_url,
                "bond_amount_units": bond_units,
                "operator": operator,
                "explorer": ARC_EXPLORER + reg["register_tx"],
            }
        )

    summary = {
        "registry_addr": registry_addr,
        "constitution_registry": constitution_registry,
        "bond_vault": bond_vault,
        "identity_registry": identity_registry,
        "operator": operator,
        "chain_id": cid,
        "bond_usdc_each": bond_usdc,
        "seeded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "note": (
            "All agents share one operator key for the demo; each is a "
            "distinct on-chain entity (own ERC-8004 identity + AgentRegistry "
            "agentId). One agent per identity (enforced on-chain)."
        ),
        "agents": agents,
    }

    if write_path is not None:
        write_path.parent.mkdir(parents=True, exist_ok=True)
        write_path.write_text(json.dumps(summary, indent=2) + "\n")
        summary["arena_json"] = str(write_path)

    return summary


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _resolve_pk(args: argparse.Namespace) -> str:
    """Resolve the operator key in-process. Priority: --pk > keystore --account
    (or $DEPLOYER_ACCOUNT) > $DEPLOYER_PK. Never logged, never in argv."""
    if args.pk:
        return args.pk
    from scripts.lib.keys import KeyResolutionError, resolve_deployer_key

    try:
        return resolve_deployer_key(account=args.account, allow_interactive=True)
    except KeyResolutionError as e:
        # Never print the key/password — only the actionable guidance.
        raise SystemExit(f"REFUSING: {e}")


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(
        description=(
            "Seed N agents into a deployed AgentRegistry: per agent, mint an "
            "ERC-8004 identity, post a bond, define a constitution, then "
            "AgentRegistry.register. Real txs via the in-process signer."
        )
    )
    p.add_argument("--rpc-url", default=None, help="Chain RPC (or $RPC).")
    p.add_argument("--registry", required=True, help="AgentRegistry address.")
    p.add_argument("--bond-vault", required=True, help="BondVault address.")
    p.add_argument(
        "--constitution-registry",
        required=True,
        help="ConstitutionRegistry address.",
    )
    p.add_argument("--n", type=int, default=3, help="Number of agents (default 3).")
    p.add_argument(
        "--bond-usdc", type=float, default=1.0, help="Bond per agent in USDC."
    )
    p.add_argument(
        "--identity-registry",
        default=ARC_IDENTITY_REGISTRY_DEFAULT,
        help="ERC-8004 identity registry (default Arc 0x8004…).",
    )
    p.add_argument(
        "--dark-pool-url",
        default="http://localhost:8000/pool",
        help="Base dark-pool URL each agent advertises.",
    )
    p.add_argument("--pk", default=None, help="DEPLOYER_PK (discouraged; prefer --account).")
    p.add_argument(
        "--account",
        default=None,
        help="Encrypted Foundry keystore name in ~/.foundry/keystores/.",
    )
    p.add_argument(
        "--out",
        default=str(ARENA_JSON),
        help="Where to write the JSON summary (default deployments/arena.json).",
    )
    args = p.parse_args(argv)

    rpc_url = args.rpc_url or os.environ.get("RPC", "").strip()
    if not rpc_url:
        print("REFUSING: need --rpc-url or $RPC.", file=sys.stderr)
        return 3

    pk = _resolve_pk(args)

    summary = seed_arena(
        rpc_url=rpc_url,
        pk=pk,
        registry_addr=args.registry,
        bond_vault=args.bond_vault,
        constitution_registry=args.constitution_registry,
        n=args.n,
        bond_usdc=args.bond_usdc,
        identity_registry=args.identity_registry,
        dark_pool_url_base=args.dark_pool_url,
        write_path=Path(args.out),
    )

    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
