"""provision.py — turn spawned agent keypairs into REAL on-chain agents.

Task A2. ``agents.agent_wallet.spawn_keypairs`` mints N fresh EVM keypairs in
memory. Those agents are inert until they exist on-chain. ``provision_agents``
brings each one to life on Arc, in this order, so the AGENT — not the operator —
owns everything it should:

  1. OPERATOR funds the agent with a little USDC. On Arc, USDC
     (``0x3600…0000``) is BOTH the ERC-20 settlement asset AND the native gas
     token, so this one transfer covers the agent's gas for the next two
     self-signed txs PLUS its Colosseum stake. Signed by the operator.

  2. The agent SELF-MINTS an ERC-8004 identity by calling the IdentityRegistry's
     ``register(string,(string,bytes)[])`` with its OWN key. Because
     ``msg.sender`` becomes the identity owner, the agent owns its identity — not
     the operator. The minted ``agentId`` is written back onto the wallet.

  3. The agent ``approve``s the Colosseum to pull its USDC stake, then
     self-``registerAgent(self)`` — both signed by its OWN key. In
     ``Colosseum.registerAgent`` (see contracts/src/Colosseum.sol) the caller
     becomes ``info.developer`` and the stake is pulled via
     ``safeTransferFrom(msg.sender, …)``. Signing with the agent's key therefore
     makes the agent its own developer, with skin in the game.

TESTABILITY (mirrors ``DuelRunner``'s injected ``send_fn``): the two chain
side-effects are injectable. ``send_fn(pk, to, sig, args) -> receipt`` and
``register_identity_fn(pk, agent_uri) -> identity_id`` default to thin wrappers
over the real ``scripts.lib.chain`` / ``scripts.demo_e2e`` helpers, but a unit
test passes fakes so the orchestration/sequence is verified with NO network.

SECURITY: the agent private keys live only in the ``AgentWallet`` objects passed
in; they are forwarded to the (in-process-signing) ``cast_send`` and never logged
or placed on argv — same contract as ``scripts.lib.chain``.
"""

from __future__ import annotations

from typing import Callable, Optional, Sequence

from agents.agent_wallet import AgentWallet

# Arc USDC: ERC-20 settlement asset AND native gas token, 6 decimals.
USDC_ADDR = "0x3600000000000000000000000000000000000000"
USDC_DECIMALS = 6

# Canonical Arc ERC-8004 IdentityRegistry (verified on-chain — see
# scripts/demo_e2e.register_identity). Only exists on Arc / an Arc fork.
DEFAULT_REGISTRY = "0x8004A818BFB912233c491871b3d84c89A494BD9e"

# Default agent metadata URI minted into each ERC-8004 identity (same IPFS doc
# scripts/demo_e2e uses).
DEFAULT_AGENT_URI = "ipfs://bafkreibdi6623n3xpf7ymk62ckb4bo75o3qemwkpfvp5i25j66itxvsoei"

# send_fn(pk, to, sig, args) -> receipt dict.
SendFn = Callable[[str, str, str, list], dict]
# register_identity_fn(pk, agent_uri) -> minted identity id (int).
RegisterIdentityFn = Callable[[str, str], int]


def usdc_units(usdc: float) -> int:
    """Convert a USDC float amount to 6-decimal integer units (1.0 -> 1_000_000)."""
    return round(usdc * 10**USDC_DECIMALS)


def _default_send_fn(rpc_url: str) -> SendFn:
    """Real ``send_fn``: sign+broadcast via ``cast_send``, wait for the receipt.

    Signs in-process with ``pk`` (never on argv), then blocks on the receipt so
    each step is mined before the next — the ordering matters (fund before the
    agent can pay gas; approve before registerAgent pulls the stake).
    """
    from scripts.lib.chain import cast_send, wait_for_receipt

    def send(pk: str, to: str, sig: str, args: list) -> dict:
        tx = cast_send(rpc_url=rpc_url, pk=pk, to=to, sig=sig, args=[str(a) for a in args])
        receipt = wait_for_receipt(rpc_url, tx, timeout=90.0)
        if int(receipt.get("status", "0x0"), 16) != 1:
            raise RuntimeError(
                f"tx {tx} reverted (status {receipt.get('status')}): "
                f"to={to} sig={sig}"
            )
        return receipt

    return send


def _default_register_identity_fn(rpc_url: str, registry: str) -> RegisterIdentityFn:
    """Real ``register_identity_fn``: self-mint an ERC-8004 identity owned by ``pk``."""
    from scripts.demo_e2e import register_identity

    def mint(pk: str, agent_uri: str) -> int:
        result = register_identity(
            rpc_url=rpc_url, pk=pk, registry_addr=registry, agent_uri=agent_uri
        )
        return int(result["identity_id"])

    return mint


def provision_agent(
    wallet: AgentWallet,
    *,
    operator_pk: str,
    colosseum: str,
    send_fn: SendFn,
    register_identity_fn: RegisterIdentityFn,
    fund_units: int,
    stake_units: int,
    agent_uri: str = DEFAULT_AGENT_URI,
) -> AgentWallet:
    """Provision a SINGLE agent. Steps (in strict order):

      1. operator ``transfer(agent, fund_units)`` on USDC  (signed by operator)
      2. agent self-mints ERC-8004 identity                (signed by agent)
      3. agent ``approve(colosseum, stake_units)`` on USDC (signed by agent)
      4. agent ``registerAgent(agent)`` on Colosseum       (signed by agent)

    Mutates ``wallet.identity_id`` with the minted id and returns the wallet.
    """
    # 1. Operator disburses USDC to the agent (gas + stake). Operator signs.
    send_fn(operator_pk, USDC_ADDR, "transfer(address,uint256)", [wallet.address, str(fund_units)])

    # 2. Agent self-mints its ERC-8004 identity (agent signs → agent owns it).
    identity_id = register_identity_fn(wallet.private_key, agent_uri)
    wallet.identity_id = int(identity_id)

    # 3. Agent approves the Colosseum to pull its stake (agent signs).
    send_fn(wallet.private_key, USDC_ADDR, "approve(address,uint256)", [colosseum, str(stake_units)])

    # 4. Agent self-registers in the Colosseum (agent signs → agent == developer).
    send_fn(wallet.private_key, colosseum, "registerAgent(address)", [wallet.address])

    return wallet


def provision_agents(
    wallets: Sequence[AgentWallet],
    *,
    rpc_url: str,
    operator_pk: str,
    colosseum: str,
    registry: str = DEFAULT_REGISTRY,
    fund_usdc: float = 1.0,
    stake_usdc: float = 1.0,
    agent_uri: str = DEFAULT_AGENT_URI,
    send_fn: Optional[SendFn] = None,
    register_identity_fn: Optional[RegisterIdentityFn] = None,
) -> list[AgentWallet]:
    """Provision each wallet on Arc into a real autonomous on-chain agent.

    For every wallet, in order: operator funds it, the agent self-mints its
    ERC-8004 identity, then the agent approves + self-registers in the Colosseum.

    The chain side-effects are injectable for testing. By default they wrap the
    real in-process-signing helpers:
      * ``send_fn(pk, to, sig, args) -> receipt``           → ``cast_send`` + wait
      * ``register_identity_fn(pk, agent_uri) -> identity`` → ``register_identity``

    Amounts are USDC floats, converted to 6-dec units. ``fund_usdc`` must cover
    the agent's gas (USDC is the gas token on Arc) PLUS its ``stake_usdc`` stake.

    Returns the same wallet objects, each with ``identity_id`` set.
    """
    if send_fn is None:
        send_fn = _default_send_fn(rpc_url)
    if register_identity_fn is None:
        register_identity_fn = _default_register_identity_fn(rpc_url, registry)

    fund_units = usdc_units(fund_usdc)
    stake_units = usdc_units(stake_usdc)
    if fund_units < stake_units:
        raise ValueError(
            f"fund_usdc ({fund_usdc}) must cover the stake ({stake_usdc}) plus gas; "
            f"got fund_units={fund_units} < stake_units={stake_units}"
        )

    provisioned: list[AgentWallet] = []
    for wallet in wallets:
        provisioned.append(
            provision_agent(
                wallet,
                operator_pk=operator_pk,
                colosseum=colosseum,
                send_fn=send_fn,
                register_identity_fn=register_identity_fn,
                fund_units=fund_units,
                stake_units=stake_units,
                agent_uri=agent_uri,
            )
        )
    return provisioned
