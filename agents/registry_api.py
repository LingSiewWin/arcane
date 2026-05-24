"""registry_api.py — the Agent Arena multi-agent service (sub-project 2).

This is the M2M layer that lets agents register an on-chain identity, publish
+ sell reasoning alpha, and be scored by the real oracle — without a human in
the loop. It sits on top of three already-shipped pieces:

  * ``contracts/src/AgentRegistry.sol`` — the on-chain source of truth + the
    live ``AgentAction`` event stream the UI (sub-project 3) watches.
  * ``agents/dark_pool.py`` — the x402-paywalled shared ``MemoryService``
    (the "dark pool"). We compose a ``DarkPoolServer`` here and share its
    ``MemoryService`` so advice published through the registry API lands in
    the SAME index that paid ``/query`` reads from.
  * ``scripts/lib/chain.py`` / ``scripts/lib/keys.py`` — in-process signing.
    The deployer/operator key NEVER reaches argv or a child process env; the
    transaction is signed locally with ``eth_account`` and broadcast via
    ``eth_sendRawTransaction`` (see chain.py's security notes).

ENDPOINTS
---------
  * ``POST /register``           — sign+send ``AgentRegistry.register(...)``.
  * ``GET  /agents``             — read ``agentCount`` + ``getAgent(i)`` for the
                                   whole directory; reputation = win/loss from
                                   the PerformanceOracle resolve history if a
                                   ``performance_oracle`` is configured, else 0/0.
  * ``POST /agents/{id}/advice`` — add a real reasoning trace to the shared
                                   dark-pool ``MemoryService``, then send
                                   ``recordAction(id, 0, payload)``.
  * ``POST /agents/{id}/resolve``— fetch a real Hermes VAA, call
                                   ``PerformanceOracle.resolve``, and on
                                   slash/release send ``recordAction(id, 3|4)``.
  * The composed dark pool is mounted under ``/pool`` so ``POST /pool/query``
    is the x402-paid query path (unchanged handlers from ``dark_pool.py``).

DATA-SOURCE HONESTY
-------------------
No mocks. Advice traces are embedded with the SAME MiniLM model the dark pool
uses (falling back to the deterministic ``hash_to_vec`` only when sentence-
transformers isn't installed, exactly like ``agents/bob.py``). Directory reads
come straight off-chain. An empty registry returns an honest empty list — never
fabricated rows.
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from typing import Any, Optional

import numpy as np
from eth_abi import decode as abi_decode
from eth_abi import encode as abi_encode
from eth_utils import keccak, to_canonical_address
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from agents.dark_pool import DarkPoolServer
from agents.memory_service import MemoryService, hash_to_vec
from scripts.lib.chain import cast_send, rpc_call, wait_for_receipt

# Action kinds — mirror the AgentAction enum in AgentRegistry.sol.
KIND_ADVICE_PUBLISHED = 0
KIND_QUERY_PAID = 1
KIND_CONSTITUTION_REVERT = 2
KIND_BOND_SLASHED = 3
KIND_BOND_RELEASED = 4

# Same embedder family Alice/Bob/the dark pool use. Real MiniLM by default; the
# deterministic ``hash_to_vec`` fallback keeps the service runnable (and tests
# fast) without dragging torch in. See agents/bob.py for the same pattern.
DEFAULT_EMBED_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
DEFAULT_EMBED_DIM = 384

# Event topic0 for AgentAction(uint256,uint8,bytes,uint256).
AGENT_ACTION_TOPIC = "0x" + keccak(
    b"AgentAction(uint256,uint8,bytes,uint256)"
).hex()


# ---------------------------------------------------------------------------
# ABI encoding for AgentRegistry calls (pure Python; no cast dependency).
# ---------------------------------------------------------------------------


def encode_register(
    identity_id: int,
    constitution_hash: bytes,
    bond_vault: str,
    dark_pool_url: str,
) -> str:
    """ABI-encode ``register(uint256,bytes32,address,string)``. Returns 0x hex."""
    if len(constitution_hash) != 32:
        raise ValueError("constitution_hash must be 32 bytes")
    sel = keccak(b"register(uint256,bytes32,address,string)")[:4]
    body = abi_encode(
        ["uint256", "bytes32", "address", "string"],
        [
            int(identity_id),
            constitution_hash,
            to_canonical_address(bond_vault),
            dark_pool_url,
        ],
    )
    return "0x" + (sel + body).hex()


def encode_record_action(agent_id: int, kind: int, payload: bytes) -> str:
    """ABI-encode ``recordAction(uint256,uint8,bytes)``. Returns 0x hex."""
    if not 0 <= int(kind) <= 255:
        raise ValueError("kind must fit in uint8")
    sel = keccak(b"recordAction(uint256,uint8,bytes)")[:4]
    body = abi_encode(
        ["uint256", "uint8", "bytes"],
        [int(agent_id), int(kind), bytes(payload)],
    )
    return "0x" + (sel + body).hex()


# --- Invocation-trace payload (ADVICE_PUBLISHED, kind=0) --------------------
# The AgentAction `payload` is opaque bytes, so we can put the FULL invocation
# trace on-chain — no contract change, fully arcscan-verifiable from the public
# RPC. We commit:
#   abi.encode(string reasoning, string symbol, string stance, bytes32 adviceHash)
# - reasoning: the real reasoning text (the same string embedded into memory).
# - symbol/stance: the asset + direction — the herding signal the 3D clustering
#   groups on (correlated actions = same symbol + stance in a window).
# - adviceHash: keccak(reasoning) — the dark-pool memory commitment.
# Legacy actions carry a bare 32-byte keccak(trace); decoders fall back to that.
_ADVICE_PAYLOAD_TYPES = ["string", "string", "string", "bytes32"]
# Recognised stances (direction of the action). "neutral" is the safe default.
ADVICE_STANCES = ("long", "exit", "vol", "reduce", "neutral")


def encode_advice_payload(reasoning: str, symbol: str, stance: str) -> bytes:
    """ABI-encode the on-chain invocation trace for an ADVICE_PUBLISHED action.

    adviceHash is always keccak(reasoning) so the on-chain commitment matches
    what lands in the shared memory index.
    """
    sym = (symbol or "").upper()
    stc = stance if stance in ADVICE_STANCES else "neutral"
    advice_hash = keccak(reasoning.encode())
    return abi_encode(
        _ADVICE_PAYLOAD_TYPES, [reasoning, sym, stc, advice_hash]
    )


def decode_advice_payload(payload: bytes | str) -> Optional[dict[str, Any]]:
    """Decode an ADVICE_PUBLISHED payload back into its trace fields.

    Returns ``{"reasoning", "symbol", "stance", "advice_hash"}`` or ``None`` if
    the bytes aren't the structured trace (e.g. a legacy bare keccak hash).
    """
    raw = payload if isinstance(payload, (bytes, bytearray)) else bytes.fromhex(
        str(payload).removeprefix("0x")
    )
    # A legacy bare hash is exactly 32 bytes and never abi-decodes as 4 dynamic
    # fields, but guard explicitly so we never misread it as a 1-char string.
    if len(raw) <= 32:
        return None
    try:
        reasoning, symbol, stance, advice_hash = abi_decode(_ADVICE_PAYLOAD_TYPES, raw)
    except Exception:
        return None
    return {
        "reasoning": reasoning,
        "symbol": symbol,
        "stance": stance,
        "advice_hash": "0x" + bytes(advice_hash).hex(),
    }


def encode_agent_count() -> str:
    return "0x" + keccak(b"agentCount()")[:4].hex()


def encode_get_agent(agent_id: int) -> str:
    sel = keccak(b"getAgent(uint256)")[:4]
    body = abi_encode(["uint256"], [int(agent_id)])
    return "0x" + (sel + body).hex()


# The Agent tuple layout from AgentRegistry.getAgent(uint256):
#   (uint256 identityId, bytes32 constitutionHash, address bondVault,
#    string darkPoolUrl, address operator, uint64 registeredAt, bool active)
_AGENT_TUPLE = "(uint256,bytes32,address,string,address,uint64,bool)"


def decode_agent_tuple(return_hex: str) -> dict[str, Any]:
    """Decode the ``getAgent`` return bytes into a directory dict."""
    raw = bytes.fromhex(return_hex.removeprefix("0x"))
    (
        identity_id,
        constitution_hash,
        bond_vault,
        dark_pool_url,
        operator,
        registered_at,
        active,
    ) = abi_decode([_AGENT_TUPLE], raw)[0]
    return {
        "identity_id": int(identity_id),
        "constitution_hash": "0x" + bytes(constitution_hash).hex(),
        "bond_vault": "0x" + bytes(to_canonical_address(bond_vault)).hex(),
        "dark_pool_url": dark_pool_url,
        "operator": "0x" + bytes(to_canonical_address(operator)).hex(),
        "registered_at": int(registered_at),
        "active": bool(active),
    }


def find_agent_id_in_receipt(receipt: dict, registry_addr: str) -> Optional[int]:
    """Pull the freshly-minted agentId out of the AgentRegistered event.

    ``AgentRegistered(uint256 indexed agentId, uint256 indexed identityId,
    address indexed operator, bytes32 constitutionHash)`` — agentId is topic[1].
    """
    topic0 = "0x" + keccak(
        b"AgentRegistered(uint256,uint256,address,bytes32)"
    ).hex()
    for lg in receipt.get("logs", []) or []:
        if (lg.get("address") or "").lower() != registry_addr.lower():
            continue
        topics = lg.get("topics") or []
        if not topics or topics[0].lower() != topic0.lower():
            continue
        if len(topics) < 2:
            continue
        return int(topics[1], 16)
    return None


def find_agent_action_in_receipt(
    receipt: dict, registry_addr: str
) -> Optional[dict]:
    """Decode the AgentAction event from a recordAction receipt, if present.

    Returns ``{"agent_id", "kind", "payload", "timestamp"}`` or ``None``.
    """
    for lg in receipt.get("logs", []) or []:
        if (lg.get("address") or "").lower() != registry_addr.lower():
            continue
        topics = lg.get("topics") or []
        if not topics or topics[0].lower() != AGENT_ACTION_TOPIC.lower():
            continue
        agent_id = int(topics[1], 16) if len(topics) > 1 else None
        kind = int(topics[2], 16) if len(topics) > 2 else None
        data_hex = (lg.get("data") or "0x").removeprefix("0x")
        payload, timestamp = abi_decode(
            ["bytes", "uint256"], bytes.fromhex(data_hex)
        )
        return {
            "agent_id": agent_id,
            "kind": kind,
            "payload": "0x" + bytes(payload).hex(),
            "timestamp": int(timestamp),
        }
    return None


# ---------------------------------------------------------------------------
# Embedding — real MiniLM, deterministic fallback (same as agents/bob.py).
# ---------------------------------------------------------------------------


class _Embedder:
    """Lazily-loaded MiniLM embedder with a deterministic fallback.

    ``embedding_model=None`` forces the deterministic ``hash_to_vec`` path —
    used by tests so we don't pay torch's import cost or require model weights.
    """

    def __init__(
        self,
        model_name: Optional[str] = DEFAULT_EMBED_MODEL,
        dim: int = DEFAULT_EMBED_DIM,
        seed: int = 0,
    ) -> None:
        self.model_name = model_name
        self.dim = int(dim)
        self.seed = int(seed)
        self._model = None
        self._lock = threading.Lock()

    def embed(self, text: str) -> np.ndarray:
        if self.model_name is None:
            return hash_to_vec(text, dim=self.dim, seed=self.seed)
        with self._lock:
            if self._model is None:
                from sentence_transformers import SentenceTransformer

                self._model = SentenceTransformer(self.model_name)
        emb = self._model.encode(
            [text], normalize_embeddings=True, show_progress_bar=False
        )[0].astype(np.float32)
        if emb.shape[0] != self.dim:
            raise RuntimeError(
                f"embedding dim mismatch: got {emb.shape[0]}, want {self.dim}"
            )
        return emb


# ---------------------------------------------------------------------------
# Request bodies.
# ---------------------------------------------------------------------------


class RegisterBody(BaseModel):
    identity_id: int = Field(..., description="ERC-8004 identity NFT id the signer owns.")
    constitution_hash: str = Field(..., description="0x-prefixed 32-byte hash.")
    dark_pool_url: str = Field(..., description="Agent's M2M endpoint.")
    bond_vault: str = Field(..., description="BondVault address with a posted bond.")
    registry_addr: Optional[str] = Field(
        None, description="Override the service's default AgentRegistry address."
    )


class AdviceBody(BaseModel):
    trace: str = Field(..., description="The reasoning trace text (real, templated).")
    vec: Optional[list[float]] = Field(
        None, description="Optional precomputed embedding; else MiniLM-embedded server-side."
    )
    payload: Optional[str] = Field(
        None,
        description="0x-prefixed bytes override for recordAction; else the "
        "structured invocation-trace payload (reasoning+symbol+stance+hash).",
    )
    trace_id: Optional[str] = Field(None, description="Memory trace id; else derived.")
    kind: str = Field("working", description="Memory kind (working/episodic/semantic).")
    symbol: Optional[str] = Field(
        None, description="Asset the action concerns (e.g. SOL) — the clustering signal."
    )
    stance: Optional[str] = Field(
        None, description="Action direction: long/exit/vol/reduce/neutral."
    )


class ResolveBody(BaseModel):
    oracle_addr: Optional[str] = Field(None, description="PerformanceOracle address.")
    agent_addr: Optional[str] = Field(
        None, description="The on-chain agent address whose advice to resolve."
    )
    feed_id: Optional[str] = Field(None, description="Pyth feed id (defaults to SOL/USD).")


# ---------------------------------------------------------------------------
# The service.
# ---------------------------------------------------------------------------


@dataclass
class RegistryConfig:
    rpc_url: str
    registry_addr: str
    # Signing material — exactly one path is used at request time.
    deployer_pk: Optional[str] = None
    deployer_account: Optional[str] = None
    chain_id: int = 5042002
    # x402 / dark-pool config (passed through to the composed DarkPoolServer).
    payment_recipient: Optional[str] = None
    usdc_address: str = "0x3600000000000000000000000000000000000000"
    price_per_query_usdc: str = "0.001"
    # Optional oracle wiring for reputation + resolve.
    performance_oracle: Optional[str] = None
    pyth_addr: Optional[str] = None
    # Embedding — None model_name => deterministic hash_to_vec (tests).
    embedding_model: Optional[str] = DEFAULT_EMBED_MODEL
    embedding_dim: int = DEFAULT_EMBED_DIM


class RegistryService:
    """FastAPI service that turns AgentRegistry into a live multi-agent arena.

    Owns: the shared ``MemoryService``, a composed ``DarkPoolServer`` (mounted
    at ``/pool``), the on-chain signer resolution, and the four arena
    endpoints. Read paths use ``eth_call``; write paths sign in-process via
    ``scripts.lib.chain.cast_send`` (raw calldata we ABI-encode here).
    """

    def __init__(
        self,
        config: RegistryConfig,
        *,
        memory: Optional[MemoryService] = None,
        dark_pool: Optional[DarkPoolServer] = None,
    ) -> None:
        self.config = config
        self.memory = memory or MemoryService(dim=config.embedding_dim)
        self._embedder = _Embedder(
            model_name=config.embedding_model,
            dim=config.embedding_dim,
        )

        # Compose the dark pool over the SAME MemoryService so advice published
        # through /agents/{id}/advice is queryable via the paid /pool/query.
        if dark_pool is None and config.payment_recipient:
            dark_pool = DarkPoolServer(
                memory=self.memory,
                price_per_query_usdc=config.price_per_query_usdc,
                payment_recipient=config.payment_recipient,
                arc_chain_id=config.chain_id,
                usdc_address=config.usdc_address,
            )
        self.dark_pool = dark_pool

        self.app = FastAPI(title="AgoraHack Agent Arena Registry")
        self._register_routes()
        if self.dark_pool is not None:
            self.app.mount("/pool", self.dark_pool.app)

    # ---- signing -------------------------------------------------------

    def _resolve_pk(self) -> str:
        """Resolve the signer key in-process. Never logged, never in argv."""
        if self.config.deployer_pk:
            return self.config.deployer_pk
        from scripts.lib.keys import resolve_deployer_key

        return resolve_deployer_key(account=self.config.deployer_account)

    def _send(self, to: str, data: str, *, value: int = 0, gas_limit: int | None = None) -> str:
        return cast_send(
            rpc_url=self.config.rpc_url,
            pk=self._resolve_pk(),
            to=to,
            data=data,
            value=value,
            gas_limit=gas_limit,
        )

    def _eth_call(self, to: str, data: str) -> str:
        return rpc_call(
            self.config.rpc_url,
            "eth_call",
            [{"to": to, "data": data}, "latest"],
        )

    # ---- read paths ----------------------------------------------------

    def agent_count(self, registry_addr: Optional[str] = None) -> int:
        addr = registry_addr or self.config.registry_addr
        out = self._eth_call(addr, encode_agent_count())
        return int(out, 16) if out and out != "0x" else 0

    def get_agent(self, agent_id: int, registry_addr: Optional[str] = None) -> dict:
        addr = registry_addr or self.config.registry_addr
        out = self._eth_call(addr, encode_get_agent(agent_id))
        agent = decode_agent_tuple(out)
        agent["agent_id"] = int(agent_id)
        agent["reputation"] = self._reputation_for(agent)
        return agent

    def list_agents(self, registry_addr: Optional[str] = None) -> list[dict]:
        """Assemble the full directory. Empty registry => honest empty list."""
        addr = registry_addr or self.config.registry_addr
        count = self.agent_count(addr)
        return [self.get_agent(i, addr) for i in range(1, count + 1)]

    def _reputation_for(self, agent: dict) -> dict[str, int]:
        """Reputation v1 = win/loss from PerformanceOracle resolve history.

        We derive wins/losses by scanning ``AdviceResolved`` events for the
        agent's operator address. ``slashed=True`` is a loss; ``slashed=False``
        is a win. With no oracle configured (or no history), returns 0/0 — an
        honest empty reputation, never fabricated.
        """
        oracle = self.config.performance_oracle
        if not oracle:
            return {"wins": 0, "losses": 0}
        try:
            wins, losses = self._scan_resolve_history(oracle, agent["operator"])
        except Exception:
            return {"wins": 0, "losses": 0}
        return {"wins": wins, "losses": losses}

    def _scan_resolve_history(self, oracle: str, operator: str) -> tuple[int, int]:
        """Count win/loss from AdviceResolved(address indexed agent, ...) logs."""
        topic0 = "0x" + keccak(
            b"AdviceResolved(address,int64,int64,int256,bool)"
        ).hex()
        agent_topic = "0x" + ("0" * 24) + operator.lower().removeprefix("0x")
        logs = rpc_call(
            self.config.rpc_url,
            "eth_getLogs",
            [
                {
                    "fromBlock": "0x0",
                    "toBlock": "latest",
                    "address": oracle,
                    "topics": [topic0, agent_topic],
                }
            ],
        )
        wins = losses = 0
        for lg in logs or []:
            data_hex = (lg.get("data") or "0x").removeprefix("0x")
            try:
                _p0, _p1, _r, slashed = abi_decode(
                    ["int64", "int64", "int256", "bool"], bytes.fromhex(data_hex)
                )
            except Exception:
                continue
            if bool(slashed):
                losses += 1
            else:
                wins += 1
        return wins, losses

    # ---- write paths ---------------------------------------------------

    def register_agent(self, body: RegisterBody) -> dict:
        ch = body.constitution_hash
        ch_bytes = bytes.fromhex(ch.removeprefix("0x"))
        registry_addr = body.registry_addr or self.config.registry_addr
        data = encode_register(
            body.identity_id, ch_bytes, body.bond_vault, body.dark_pool_url
        )
        tx_hash = self._send(registry_addr, data)
        receipt = wait_for_receipt(self.config.rpc_url, tx_hash, timeout=90.0)
        if int(receipt.get("status", "0x0"), 16) != 1:
            raise RuntimeError(f"register reverted (tx {tx_hash})")
        agent_id = find_agent_id_in_receipt(receipt, registry_addr)
        return {"agent_id": agent_id, "tx_hash": tx_hash}

    def publish_advice(self, agent_id: int, body: AdviceBody) -> dict:
        # 1. Embed the trace (real MiniLM, or deterministic fallback).
        if body.vec is not None:
            vec = np.asarray(body.vec, dtype=np.float32)
            if vec.shape != (self.memory.dim,):
                raise ValueError(
                    f"vec must be length {self.memory.dim}, got {vec.shape}"
                )
        else:
            vec = self._embedder.embed(body.trace)

        # 2. Add to the SHARED dark-pool memory so /pool/query can retrieve it.
        trace_id = body.trace_id or ("advice-" + keccak(body.trace.encode()).hex()[:16])
        self.memory.add(
            trace_id=trace_id,
            vec=vec,
            kind=body.kind,
            payload={"agent_id": int(agent_id), "trace": body.trace},
        )

        # 3. Build the on-chain payload. Default = the full structured invocation
        #    trace (reasoning+symbol+stance+adviceHash), so the UI can render and
        #    verify the whole trace straight from the public RPC. An explicit
        #    `payload` override still wins (e.g. for tests / custom commitments).
        if body.payload is not None:
            payload_bytes = bytes.fromhex(body.payload.removeprefix("0x"))
        else:
            payload_bytes = encode_advice_payload(
                body.trace, body.symbol or "", body.stance or "neutral"
            )

        # 4. recordAction(agentId, ADVICE_PUBLISHED, payload).
        data = encode_record_action(agent_id, KIND_ADVICE_PUBLISHED, payload_bytes)
        tx_hash = self._send(self.config.registry_addr, data)
        receipt = wait_for_receipt(self.config.rpc_url, tx_hash, timeout=90.0)
        if int(receipt.get("status", "0x0"), 16) != 1:
            raise RuntimeError(f"recordAction reverted (tx {tx_hash})")
        event = find_agent_action_in_receipt(receipt, self.config.registry_addr)
        return {
            "tx_hash": tx_hash,
            "trace_id": trace_id,
            "memory_entries": len(self.memory),
            "event": event,
        }

    def record_query_paid(self, agent_id: int, payload: bytes = b"") -> dict:
        """Emit a QUERY_PAID action (kind=1) — used after a paid /pool/query."""
        data = encode_record_action(agent_id, KIND_QUERY_PAID, payload)
        tx_hash = self._send(self.config.registry_addr, data)
        receipt = wait_for_receipt(self.config.rpc_url, tx_hash, timeout=90.0)
        if int(receipt.get("status", "0x0"), 16) != 1:
            raise RuntimeError(f"recordAction(QUERY_PAID) reverted (tx {tx_hash})")
        return {
            "tx_hash": tx_hash,
            "event": find_agent_action_in_receipt(receipt, self.config.registry_addr),
        }

    def resolve_agent(self, agent_id: int, body: ResolveBody) -> dict:
        """Fetch a real Hermes VAA, resolve via PerformanceOracle, then record.

        On ``slashed=True`` emits BOND_SLASHED (kind=3); otherwise BOND_RELEASED
        (kind=4). Reuses ``scripts.resolve_bond.resolve_bond`` so the Hermes +
        Pyth + on-chain resolution path is the SAME real one the CLI uses.
        """
        from scripts.resolve_bond import SOL_USD_FEED, resolve_bond

        oracle = body.oracle_addr or self.config.performance_oracle
        if not oracle:
            raise ValueError("no PerformanceOracle configured")
        agent_addr = body.agent_addr
        if not agent_addr:
            raise ValueError("agent_addr (on-chain advice owner) is required")

        kwargs: dict[str, Any] = dict(
            rpc_url=self.config.rpc_url,
            pk=self._resolve_pk(),
            oracle_addr=oracle,
            agent=agent_addr,
            feed_id=body.feed_id or SOL_USD_FEED,
        )
        if self.config.pyth_addr:
            kwargs["pyth_addr"] = self.config.pyth_addr
        result = resolve_bond(**kwargs)

        resolved = result.get("advice_resolved") or {}
        slashed = bool(resolved.get("slashed"))
        kind = KIND_BOND_SLASHED if slashed else KIND_BOND_RELEASED
        # Payload = abi.encode(int256 r_bps) so the UI can render the outcome.
        r_bps = int(resolved.get("r_bps", 0))
        payload = abi_encode(["int256"], [r_bps])
        data = encode_record_action(agent_id, kind, payload)
        action_tx = self._send(self.config.registry_addr, data)
        action_receipt = wait_for_receipt(self.config.rpc_url, action_tx, timeout=90.0)
        result["recordAction"] = {
            "kind": kind,
            "tx_hash": action_tx,
            "event": find_agent_action_in_receipt(
                action_receipt, self.config.registry_addr
            ),
        }
        return result

    # ---- FastAPI wiring ------------------------------------------------

    def _register_routes(self) -> None:
        @self.app.get("/health")
        async def health() -> dict[str, Any]:
            return {
                "ok": True,
                "registry_addr": self.config.registry_addr,
                "chain_id": self.config.chain_id,
                "memory_entries": len(self.memory),
                # Live proof of the memory-efficiency thesis: the genuine 1-bit
                # RaBitQ store footprint vs the FP32 baseline (bytes/vec,
                # compression). Computed from the real index, not hardcoded.
                "memory": self.memory.memory_stats(),
                "dark_pool_mounted": self.dark_pool is not None,
            }

        @self.app.post("/register")
        async def register_route(body: RegisterBody) -> Any:
            try:
                return JSONResponse(status_code=200, content=self.register_agent(body))
            except Exception as exc:  # noqa: BLE001
                return JSONResponse(status_code=400, content={"error": str(exc)})

        @self.app.get("/agents")
        async def agents_route() -> Any:
            try:
                return JSONResponse(
                    status_code=200, content={"agents": self.list_agents()}
                )
            except Exception as exc:  # noqa: BLE001
                return JSONResponse(status_code=502, content={"error": str(exc)})

        @self.app.post("/agents/{agent_id}/advice")
        async def advice_route(agent_id: int, body: AdviceBody) -> Any:
            try:
                return JSONResponse(
                    status_code=200, content=self.publish_advice(agent_id, body)
                )
            except Exception as exc:  # noqa: BLE001
                return JSONResponse(status_code=400, content={"error": str(exc)})

        @self.app.post("/agents/{agent_id}/resolve")
        async def resolve_route(agent_id: int, body: ResolveBody) -> Any:
            try:
                return JSONResponse(
                    status_code=200, content=self.resolve_agent(agent_id, body)
                )
            except Exception as exc:  # noqa: BLE001
                return JSONResponse(status_code=400, content={"error": str(exc)})


# ---------------------------------------------------------------------------
# Default-app entrypoint (uvicorn agents.registry_api:app).
# ---------------------------------------------------------------------------


def build_service_from_env() -> RegistryService:
    """Construct a RegistryService from environment variables.

    Required: ``ARENA_RPC_URL``, ``ARENA_REGISTRY_ADDR``. Signing comes from
    ``DEPLOYER_PK`` / ``DEPLOYER_ACCOUNT`` (resolved lazily at request time via
    ``scripts.lib.keys``). ``DARKPOOL_RECIPIENT`` (optional) enables the
    composed dark pool; ``ARENA_PERFORMANCE_ORACLE`` (optional) enables
    reputation + resolve.
    """
    rpc_url = os.environ.get("ARENA_RPC_URL", "").strip()
    registry_addr = os.environ.get("ARENA_REGISTRY_ADDR", "").strip()
    if not rpc_url or not registry_addr:
        raise RuntimeError(
            "ARENA_RPC_URL and ARENA_REGISTRY_ADDR are required to build the "
            "registry service."
        )
    config = RegistryConfig(
        rpc_url=rpc_url,
        registry_addr=registry_addr,
        deployer_pk=os.environ.get("DEPLOYER_PK") or None,
        deployer_account=os.environ.get("DEPLOYER_ACCOUNT") or None,
        chain_id=int(os.environ.get("ARENA_CHAIN_ID", "5042002")),
        payment_recipient=os.environ.get("DARKPOOL_RECIPIENT") or None,
        usdc_address=os.environ.get(
            "DARKPOOL_USDC_ADDRESS", "0x3600000000000000000000000000000000000000"
        ),
        price_per_query_usdc=os.environ.get("DARKPOOL_PRICE_USDC", "0.001"),
        performance_oracle=os.environ.get("ARENA_PERFORMANCE_ORACLE") or None,
        pyth_addr=os.environ.get("ARENA_PYTH_ADDR") or None,
    )
    return RegistryService(config)


_DEFAULT_SERVICE: RegistryService | None = None


def __getattr__(name: str) -> Any:
    """Lazy ``app`` so importing this module has no side effects (mirrors
    ``agents.dark_pool``'s lazy app)."""
    if name == "app":
        global _DEFAULT_SERVICE
        if _DEFAULT_SERVICE is None:
            _DEFAULT_SERVICE = build_service_from_env()
        return _DEFAULT_SERVICE.app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
