"""Tests for agents.provision — on-chain agent provisioning (Task A2).

NO network. The two chain side-effects (``send_fn`` / ``register_identity_fn``)
are injected as fakes that record every call, so we assert the exact tx SEQUENCE
and signer per agent without touching Arc:

  per agent, in order:
    1. operator  transfer(agent, fund_units)      on USDC      (signed by OP)
    2. agent     self-mint ERC-8004 identity                   (signed by agent)
    3. agent     approve(colosseum, stake_units)  on USDC      (signed by agent)
    4. agent     registerAgent(agent)             on Colosseum (signed by agent)
"""

from __future__ import annotations

from eth_account import Account

from agents.agent_wallet import spawn_keypairs
from agents.provision import (
    USDC_ADDR,
    provision_agents,
    usdc_units,
)

# Anvil well-known account #0 — the OPERATOR/funder. Test-only key.
OPERATOR_PK = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
OPERATOR_ADDR = Account.from_key(OPERATOR_PK).address

# Colosseum address — only ever string-compared by the fakes, never decoded.
COLOSSEUM = "0x00000000000000000000000000000000C0105EE1"


class FakeChain:
    """Records every send_fn / register_identity_fn call for assertions.

    send_fn signature mirrors the real one: (pk, to, sig, args) -> receipt.
    register_identity_fn: (pk, agent_uri) -> incrementing identity id.
    """

    def __init__(self) -> None:
        # Each entry: {"signer": <addr derived from pk>, "to", "sig", "args"}.
        self.sends: list[dict] = []
        # Each entry: {"signer", "agent_uri", "identity_id"}.
        self.mints: list[dict] = []
        self._next_id = 1000

    def send(self, pk: str, to: str, sig: str, args: list) -> dict:
        self.sends.append(
            {
                "signer": Account.from_key(pk).address,
                "to": to,
                "sig": sig,
                "args": list(args),
            }
        )
        return {"status": "0x1", "transactionHash": "0xfake"}

    def mint(self, pk: str, agent_uri: str) -> int:
        identity_id = self._next_id
        self._next_id += 1
        self.mints.append(
            {
                "signer": Account.from_key(pk).address,
                "agent_uri": agent_uri,
                "identity_id": identity_id,
            }
        )
        return identity_id


def test_usdc_unit_conversion():
    assert usdc_units(1.0) == 1_000_000
    assert usdc_units(0.1) == 100_000
    assert usdc_units(50.0) == 50_000_000


def test_provision_sequence_and_signers(tmp_path):
    wallets = spawn_keypairs(2, password="pw", keystore_dir=tmp_path)
    w1, w2 = wallets
    chain = FakeChain()

    out = provision_agents(
        wallets,
        rpc_url="x",  # never used — fakes are injected
        operator_pk=OPERATOR_PK,
        colosseum=COLOSSEUM,
        fund_usdc=1,
        stake_usdc=1,
        send_fn=chain.send,
        register_identity_fn=chain.mint,
    )

    # Returns the same wallets, each with an identity_id now set.
    assert out is not None and len(out) == 2
    assert w1.identity_id == 1000
    assert w2.identity_id == 1001
    # And mints were signed by the AGENTS, not the operator (agent owns identity).
    assert chain.mints[0]["signer"] == w1.address
    assert chain.mints[1]["signer"] == w2.address

    # send_fn fired 3 sends per agent (transfer, approve, registerAgent) = 6.
    assert len(chain.sends) == 6
    fund_units = usdc_units(1)   # 1_000_000
    stake_units = usdc_units(1)  # 1_000_000

    # Verify the exact ordered sequence per agent. The mints are interleaved
    # between send #1 and send #2 of each agent; assert that ordering too.
    for i, w in enumerate(wallets):
        base = i * 3
        # 1. OPERATOR funds the agent on USDC.
        s_transfer = chain.sends[base + 0]
        assert s_transfer["signer"] == OPERATOR_ADDR
        assert s_transfer["to"] == USDC_ADDR
        assert s_transfer["sig"] == "transfer(address,uint256)"
        assert s_transfer["args"] == [w.address, str(fund_units)]

        # 2. AGENT self-mints its identity (recorded in chain.mints, signed by agent).
        assert chain.mints[i]["signer"] == w.address

        # 3. AGENT approves the Colosseum to pull its stake (USDC).
        s_approve = chain.sends[base + 1]
        assert s_approve["signer"] == w.address
        assert s_approve["to"] == USDC_ADDR
        assert s_approve["sig"] == "approve(address,uint256)"
        assert s_approve["args"] == [COLOSSEUM, str(stake_units)]

        # 4. AGENT self-registers in the Colosseum (becomes its own developer).
        s_register = chain.sends[base + 2]
        assert s_register["signer"] == w.address
        assert s_register["to"] == COLOSSEUM
        assert s_register["sig"] == "registerAgent(address)"
        assert s_register["args"] == [w.address]


def test_fund_must_cover_stake(tmp_path):
    wallets = spawn_keypairs(1, password="pw", keystore_dir=tmp_path)
    chain = FakeChain()
    import pytest

    with pytest.raises(ValueError):
        provision_agents(
            wallets,
            rpc_url="x",
            operator_pk=OPERATOR_PK,
            colosseum=COLOSSEUM,
            fund_usdc=0.5,   # less than the 1.0 stake -> must raise
            stake_usdc=1.0,
            send_fn=chain.send,
            register_identity_fn=chain.mint,
        )
    # Nothing should have been broadcast on the bad-config path.
    assert chain.sends == []
    assert chain.mints == []


def test_empty_wallet_list_is_noop(tmp_path):
    chain = FakeChain()
    out = provision_agents(
        [],
        rpc_url="x",
        operator_pk=OPERATOR_PK,
        colosseum=COLOSSEUM,
        send_fn=chain.send,
        register_identity_fn=chain.mint,
    )
    assert out == []
    assert chain.sends == []
    assert chain.mints == []
