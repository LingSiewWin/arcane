"""bob — the naive agent (Slice 5A).

Bob is the demo subject. He:
  * spawns a local random EOA (Turnkey integration is Slice 5C)
  * compiles a constitution (Python dicts -> Solidity-compatible `Rule[]`)
  * queries Alice's Dark Pool via x402 (real EIP-712 signatures, real
    HTTP round-trip through Slice 4's client)
  * decides what trade to attempt and returns a structured ``TradeIntent``
    whose calldata fires the REAL rules in Slice 2's ConstitutionHook.

What he does NOT do (intentionally, see brief):
  * broadcast on Arc — that's Slice 5D
  * use real Turnkey EOAs — that's Slice 5C
  * spawn child agents via real ERC-7715 — Slice 5C

Interface (consumed by orchestrator + tests):

    bob = Bob(budget_usdc=10.0, constitution_rules=[...])
    bob.bootstrap()
    intent = bob.decide(alice_url, market_state="ETH funding flipped negative",
                        client=alice.client)
    intent.target / intent.value / intent.calldata
"""

from __future__ import annotations

import logging
import secrets
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Optional

import numpy as np
from eth_abi import encode as abi_encode
from eth_account import Account
from eth_utils import keccak

from agents.memory_service import MemoryService, _Entry, hash_to_vec
from agents.x402_client import x402_query

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants — mirror contracts/src/ConstitutionRegistry.sol
# ---------------------------------------------------------------------------

KIND_MAX_LEVERAGE = 0
KIND_MAX_TRADE_SIZE = 1
KIND_VENUE_BLACKLIST = 2
KIND_NO_UNAUDITED_CONTRACTS = 3
KIND_SUBDELEGATION_BOUND = 4
KIND_CUSTOM = 255

_KIND_STR_TO_INT = {
    "MAX_LEVERAGE": KIND_MAX_LEVERAGE,
    "MAX_TRADE_SIZE": KIND_MAX_TRADE_SIZE,
    "VENUE_BLACKLIST": KIND_VENUE_BLACKLIST,
    "NO_UNAUDITED_CONTRACTS": KIND_NO_UNAUDITED_CONTRACTS,
    "SUBDELEGATION_BOUND": KIND_SUBDELEGATION_BOUND,
    "CUSTOM": KIND_CUSTOM,
}

# Selectors — verified against the Solidity contract:
#   execute(address,uint256,bytes)         = 0xb61d27f6
#   transfer(address,uint256)              = 0xa9059cbb  (ERC-20)
#   setLeverage(uint256)                   = 0x79575b23  (Slice 2 stub — see brief)
#   issueSessionKey(address,uint256)       = 0x7873af1d  (Slice 2 stub — see brief)
EXECUTE_SELECTOR = bytes.fromhex("b61d27f6")
ERC20_TRANSFER_SELECTOR = bytes.fromhex("a9059cbb")
SET_LEVERAGE_SELECTOR = bytes.fromhex("79575b23")
ISSUE_SESSION_KEY_SELECTOR = bytes.fromhex("7873af1d")

USDC_DECIMALS = 6

# Phase 5 Stream M: ConstitutionRegistry.Rule gained an ``address adapter``
# field (see contracts/src/ConstitutionRegistry.sol). When zero the
# validator/hook falls back to inline decoding (the MAX_TRADE_SIZE ERC-20
# fast path). Bob's demo rules carry no custom adapter, so we pin it to the
# zero address — keeping Bob's locally-computed constitution hash identical
# to ``ConstitutionRegistry.hashOf`` (keccak256(abi.encode(Rule[]))).
ZERO_ADAPTER = "0x" + "0" * 40

# Default embedder. Must match Alice's seeder (``agents/seed_alice.py``) so
# Bob's query vectors land in the same space as the seeded corpus and cosine
# search returns semantically-meaningful neighbours rather than the pinned
# hash-derived slot. Tests that don't want the MiniLM dependency can opt out
# by passing ``embedding_model=None`` (falls back to deterministic
# ``hash_to_vec``) — see ``Bob._embed``.
DEFAULT_EMBED_MODEL = "sentence-transformers/all-MiniLM-L6-v2"


def _usdc_to_units(amount: float | str | Decimal) -> int:
    return int((Decimal(str(amount)) * (10**USDC_DECIMALS)).to_integral_value())


# ---------------------------------------------------------------------------
# TradeIntent — what Bob hands Slice 5D for broadcast.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TradeIntent:
    """A proposed trade, encoded so Slice 2's ConstitutionHook can evaluate it.

    Fields match the ERC-7579 ``execute(target, value, data)`` signature:
      * ``target`` — the contract Bob wants to call (ERC-20, venue router, ...)
      * ``value`` — native value forwarded (USDC raw units when used by the hook)
      * ``calldata`` — inner ``data``, the selector-prefixed call to ``target``

    ``execute_calldata`` is the full ``execute(target, value, data)`` blob the
    SCA's EntryPoint would receive — what ``ConstitutionHook.validateUserOp``
    decodes.
    """

    kind: str  # MAX_TRADE_SIZE / VENUE_BLACKLIST / NO_UNAUDITED_CONTRACTS / OK / ...
    target: str
    value: int
    calldata: bytes
    execute_calldata: bytes
    notes: str = ""
    source_trace_id: Optional[str] = None
    source_trace_score: Optional[float] = None

    @property
    def selector(self) -> bytes:
        """Inner selector. ``b""`` if the calldata is empty."""
        return self.calldata[:4] if len(self.calldata) >= 4 else b""

    def selector_hex(self) -> str:
        return "0x" + self.selector.hex() if self.selector else "0x"

    def execute_calldata_hex(self) -> str:
        return "0x" + self.execute_calldata.hex()

    def calldata_hex(self) -> str:
        return "0x" + self.calldata.hex()


# ---------------------------------------------------------------------------
# Calldata helpers — exposed so the orchestrator can build TradeIntents
# directly when it needs to (e.g. to test a specific rule's revert path).
# ---------------------------------------------------------------------------


def build_erc20_transfer_calldata(recipient: str, amount_units: int) -> bytes:
    """``transfer(address,uint256)`` calldata. Selector 0xa9059cbb."""
    return ERC20_TRANSFER_SELECTOR + abi_encode(
        ["address", "uint256"], [recipient, int(amount_units)]
    )


def build_execute_calldata(target: str, value: int, inner: bytes) -> bytes:
    """``execute(address,uint256,bytes)`` calldata.  Selector 0xb61d27f6.

    This is the outer envelope ``ConstitutionHook.validateUserOp`` decodes.
    """
    return EXECUTE_SELECTOR + abi_encode(
        ["address", "uint256", "bytes"], [target, int(value), inner]
    )


def rules_to_solidity(rules: list[dict]) -> list[tuple[int, bytes, str]]:
    """Convert Bob's Python rule dicts to Solidity
    ``(uint8 kind, bytes params, address adapter)`` tuples — the shape
    ``ConstitutionRegistry.defineConstitution`` accepts since the Phase 5
    Stream M ``adapter`` field was added.

    A rule's ``adapter`` defaults to the zero address (inline decoding); a
    rule dict may override it via an ``adapter`` key for a custom
    ``IRuleAdapter`` deployment.
    """
    out: list[tuple[int, bytes, str]] = []
    for r in rules:
        kind_label = r["kind"]
        kind_int = _KIND_STR_TO_INT.get(kind_label)
        if kind_int is None:
            raise ValueError(f"unknown rule kind: {kind_label!r}")
        params = _encode_rule_params(kind_label, r)
        adapter = r.get("adapter", ZERO_ADAPTER)
        out.append((kind_int, params, adapter))
    return out


def _encode_rule_params(kind: str, rule: dict) -> bytes:
    if kind == "MAX_LEVERAGE":
        bps = int(rule.get("max_leverage_bps", 20000))  # default 2x
        return abi_encode(["uint256"], [bps])
    if kind == "MAX_TRADE_SIZE":
        max_usdc = rule.get("max_usdc", 1.0)
        return abi_encode(["uint256"], [_usdc_to_units(max_usdc)])
    if kind == "VENUE_BLACKLIST":
        venues = list(rule.get("venues", []))
        return abi_encode(["address[]"], [venues])
    if kind == "NO_UNAUDITED_CONTRACTS":
        whitelist = list(rule.get("whitelist", []))
        return abi_encode(["address[]"], [whitelist])
    if kind == "SUBDELEGATION_BOUND":
        max_units = rule.get("max_child_budget_units")
        if max_units is None:
            max_units = _usdc_to_units(rule.get("max_child_budget_usdc", 0.5))
        return abi_encode(["uint256"], [int(max_units)])
    if kind == "CUSTOM":
        return rule.get("params_bytes", b"")
    raise ValueError(f"unknown rule kind: {kind!r}")


def constitution_hash(rules: list[dict]) -> str:
    """``keccak256(abi.encode(Rule[]))`` — matches ``ConstitutionRegistry.hashOf``.

    Encoded form matches Solidity's
    ``struct Rule { uint8 kind; bytes params; address adapter; }``.
    """
    sol_rules = rules_to_solidity(rules)
    encoded = abi_encode(["(uint8,bytes,address)[]"], [sol_rules])
    return "0x" + keccak(encoded).hex()


# ---------------------------------------------------------------------------
# Bob
# ---------------------------------------------------------------------------


@dataclass
class Bob:
    """Naive agent: budget, constitution, EOA + memory + decision loop.

    ``embedding_model`` defaults to MiniLM (matching Alice's seeded corpus,
    see ``agents/seed_alice.py``). Tests that don't want the MiniLM
    dependency can opt out by passing ``embedding_model=""`` (or the
    explicit string ``"hash"``) — both fall back to deterministic
    ``hash_to_vec`` embeddings.
    """

    budget_usdc: float = 10.0
    constitution_rules: list[dict] = field(default_factory=list)
    # Default to MiniLM so Bob's queries share Alice's embedding space.
    # Pass "" (empty) or "hash" to force the deterministic hash fallback.
    embedding_model: Optional[str] = DEFAULT_EMBED_MODEL
    embedding_dim: int = 384
    seed: int = 7

    # Filled by bootstrap()
    eoa: Optional[Account] = field(default=None, init=False)
    memory: Optional[MemoryService] = field(default=None, init=False)
    constitution_hash: Optional[str] = field(default=None, init=False)
    solidity_rules: list[tuple[int, bytes]] = field(default_factory=list, init=False)
    _embed_model = None  # lazy

    # ---- Bootstrap -----------------------------------------------------

    def bootstrap(self) -> None:
        """Generate EOA, hash constitution, init local memory.

        Idempotent: calling twice is a no-op.
        """
        if self.eoa is not None:
            return
        # Real EOA — eth_account.Account.create() uses os.urandom under the hood.
        self.eoa = Account.from_key("0x" + secrets.token_hex(32))
        self.solidity_rules = rules_to_solidity(self.constitution_rules)
        self.constitution_hash = constitution_hash(self.constitution_rules)

        # Bob's local memory — fresh, empty. Pins the constitution rules so
        # they survive decay just like Alice's pinned slot.
        self.memory = MemoryService(dim=self.embedding_dim, seed=self.seed)
        for r in self.constitution_rules:
            text = _rule_canonical_text(r)
            vec = hash_to_vec(text, dim=self.embedding_dim, seed=self.seed)
            self.memory.add(
                trace_id=f"pinned:{r.get('rule_id', r['kind'])}",
                vec=vec,
                kind="pinned",
                pinned=True,
                payload={"text": text, **r},
            )

    @property
    def address(self) -> str:
        if self.eoa is None:
            raise RuntimeError("Bob not bootstrapped")
        return self.eoa.address

    # ---- Embedding -----------------------------------------------------

    def _embed(self, text: str) -> np.ndarray:
        """Embed text. Uses MiniLM (or whatever ``embedding_model`` names) by
        default so Bob's query vectors land in the same space as Alice's
        seeded corpus and cosine search returns semantically-meaningful
        neighbours.

        The deterministic ``hash_to_vec`` fallback is available as an
        opt-out for tests that don't want the MiniLM dependency: pass
        ``embedding_model=None`` (Python None), ``""`` (empty string), or
        the explicit sentinel ``"hash"``. With the hash fallback the search
        is still real but ranking is essentially over the pinned slot —
        fine for unit tests, semantically wrong for a demo against MiniLM
        seeds.
        """
        model_name = self.embedding_model
        if model_name is None or model_name == "" or model_name.lower() == "hash":
            return hash_to_vec(text, dim=self.embedding_dim, seed=self.seed)
        if self._embed_model is None:
            from sentence_transformers import SentenceTransformer  # noqa: WPS433
            self._embed_model = SentenceTransformer(model_name)
        v = self._embed_model.encode(
            [text], normalize_embeddings=True, show_progress_bar=False
        )[0].astype(np.float32)
        return v

    # ---- Dark Pool query ----------------------------------------------

    def query_alice(
        self,
        alice_url: str,
        market_state: str,
        *,
        k: int = 5,
        chain_id: int = 5042002,
        asset_address: str = "0x3600000000000000000000000000000000000000",
        expected_price_usdc: str = "0.001",
        expected_recipient: Optional[str] = None,
        transport=None,
    ) -> list[dict]:
        """Pay ``expected_price_usdc`` USDC and ask Alice for top-k matches.

        ``transport`` is forwarded to ``x402_client.x402_query`` — pass a
        ``fastapi.testclient.TestClient`` for in-process tests, ``None`` for
        real network.  When transport is supplied, the path component is
        appended to make the relative URL.

        Phase 4 audit (B6 / N6 / P1 #8): ``expected_recipient`` is
        forwarded so the client refuses to sign for any server that
        rewrites ``payTo``. ``expected_price_usdc`` is a STRICT upper
        bound on the server's quoted ``maxAmountRequired``. Callers
        SHOULD pass ``expected_recipient`` in production — F11's
        recipient pinning is dead code without it.
        """
        if self.eoa is None:
            raise RuntimeError("Bob not bootstrapped")
        vec = self._embed(market_state)

        # When transport is a TestClient we need a relative path; when it's
        # None we need the full URL.
        url = alice_url.rstrip("/") + "/query" if alice_url else "/query"
        if transport is not None and alice_url.startswith("http"):
            # TestClient handles absolute URLs too, but in-process tests
            # typically pass an empty alice_url or path-only.
            pass
        return x402_query(
            url=url,
            query_vec=vec,
            k=k,
            signer=self.eoa,
            chain_id=chain_id,
            asset_address=asset_address,
            expected_price_usdc=expected_price_usdc,
            expected_recipient=expected_recipient,
            transport=transport,
        )

    # ---- Decision loop -------------------------------------------------

    def decide(
        self,
        alice_url: str,
        market_state: str,
        *,
        k: int = 5,
        chain_id: int = 5042002,
        asset_address: str = "0x3600000000000000000000000000000000000000",
        expected_price_usdc: str = "0.001",
        expected_recipient: Optional[str] = None,
        transport=None,
        trade_size_usdc: Optional[float] = None,
    ) -> TradeIntent:
        """Query Alice, pick the top trace, build a TradeIntent that mimics it.

        For the hackathon demo we keep the policy minimal:
          * pull top-k traces from Alice
          * mimic the top-1 — parse its size + venue from the templated text
          * if the mimicked size exceeds Bob's constitution's MAX_TRADE_SIZE,
            we KEEP the oversized number — that's the point. The
            ConstitutionHook will revert it.

        Returns the TradeIntent (an `execute(target, value, transfer(...))`
        call). Caller (Slice 5D) is responsible for broadcasting.
        """
        if self.eoa is None:
            raise RuntimeError("Bob not bootstrapped")
        results = self.query_alice(
            alice_url=alice_url,
            market_state=market_state,
            k=k,
            chain_id=chain_id,
            asset_address=asset_address,
            expected_price_usdc=expected_price_usdc,
            expected_recipient=expected_recipient,
            transport=transport,
        )
        if not results:
            raise RuntimeError("dark pool returned no traces — cannot decide")

        top = results[0]
        text = (top.get("payload") or {}).get("text", "")
        parsed = _parse_trace_text(text)

        # Override the size if the caller wants to force a violation/non-violation.
        size_usdc = (
            trade_size_usdc
            if trade_size_usdc is not None
            else parsed.get("size_usdc", 1.0)
        )

        # Persist the lesson in Bob's local memory (step 3 of the demo).
        self._remember_trace(top.get("trace_id"), text, parsed)

        return self._build_transfer_intent(
            size_usdc=size_usdc,
            top_trace_id=top.get("trace_id"),
            top_score=top.get("score"),
            top_text=text,
        )

    def _build_transfer_intent(
        self,
        *,
        size_usdc: float,
        top_trace_id: Optional[str],
        top_score: Optional[float],
        top_text: str,
    ) -> TradeIntent:
        """Build an ``execute(target, 0, transfer(recipient, amount))`` TradeIntent.

        This shape fires Slice 2's MAX_TRADE_SIZE rule on the inner ERC-20
        ``transfer(address,uint256)`` selector (0xa9059cbb) — the REAL rule,
        not the made-up setLeverage one.
        """
        # USDC contract on Arc — Bob "calls" USDC.transfer(recipient, amount).
        # Recipient is a synthetic counterparty for the demo (deterministic
        # so the demo evidence log doesn't churn between runs).
        target = "0x3600000000000000000000000000000000000000"  # USDC
        recipient = "0x000000000000000000000000000000000000bEEF"
        amount_units = _usdc_to_units(size_usdc)
        inner = build_erc20_transfer_calldata(recipient, amount_units)
        outer = build_execute_calldata(target, 0, inner)

        # Classify against Bob's own constitution so callers know what to
        # expect from the hook.
        kind = _classify_intent(
            constitution_rules=self.constitution_rules,
            target=target,
            value=0,
            inner_selector=ERC20_TRANSFER_SELECTOR,
            amount_units=amount_units,
        )

        return TradeIntent(
            kind=kind,
            target=target,
            value=0,
            calldata=inner,
            execute_calldata=outer,
            notes=(
                f"mimic {top_trace_id} (score={top_score}): "
                f"transfer {size_usdc} USDC to {recipient}. "
                f"Source text: {top_text!r}"
            ),
            source_trace_id=top_trace_id,
            source_trace_score=top_score,
        )

    def build_blacklisted_venue_intent(self, blacklisted_address: str) -> TradeIntent:
        """Construct a TradeIntent that targets a blacklisted venue.

        Shape: ``execute(blacklisted_address, 0, 0x)`` — empty inner calldata
        so only the VENUE_BLACKLIST check fires.
        """
        target = blacklisted_address
        outer = build_execute_calldata(target, 0, b"")
        return TradeIntent(
            kind="VENUE_BLACKLIST",
            target=target,
            value=0,
            calldata=b"",
            execute_calldata=outer,
            notes=f"call blacklisted venue {blacklisted_address}",
        )

    def build_unaudited_contract_intent(self, target: str) -> TradeIntent:
        """Construct a TradeIntent that targets a non-whitelisted contract.

        Shape: ``execute(target, 0, 0x)``. The NO_UNAUDITED_CONTRACTS check
        fires whenever the target isn't in the whitelist.
        """
        outer = build_execute_calldata(target, 0, b"")
        return TradeIntent(
            kind="NO_UNAUDITED_CONTRACTS",
            target=target,
            value=0,
            calldata=b"",
            execute_calldata=outer,
            notes=f"call non-whitelisted contract {target}",
        )

    # ---- Memory --------------------------------------------------------

    def _remember_trace(
        self,
        trace_id: Optional[str],
        text: str,
        parsed: dict,
    ) -> None:
        """Write the trace Bob just consumed into his own working memory.

        Used by the demo step 3 — "Bob writes the new lesson to his own memory".
        """
        if self.memory is None or trace_id is None:
            return
        vec = self._embed(text or trace_id)
        self.memory.add(
            trace_id=f"learned:{trace_id}",
            vec=vec,
            kind="working",
            pinned=False,
            payload={"text": text, "parsed": parsed, "ts": int(time.time())},
        )

    # Phase 5 Stream D (B10) — default number of WORKING entries to clone
    # from parent → child. The mandate calls for "the top-K most-recent +
    # most-frequently-retrieved working entries (use the ``weight`` field
    # as a proxy for relevance)." The MemoryService weight is exp-decayed
    # since each entry's last decay tick — recent inserts have weight ~1.0,
    # older entries have decayed. So sorting by ``weight`` descending
    # surfaces both recency AND retained-relevance (entries promoted via
    # ``decay_step`` resets) without us needing to track retrieval counts.
    DEFAULT_CHILD_WORKING_CAP: int = 64

    def spawn_child(
        self,
        *,
        child_budget_usdc: Optional[float] = None,
        extra_rules: Optional[list[dict]] = None,
        memory_working_cap: Optional[int] = None,
    ) -> "Bob":
        """Spawn a child agent with budget + constitution + parent's memory.

        Phase 5 Stream D (B10): the child inherits a deep-cloned slice of
        the parent's ``MemoryService``:

          * ALL pinned entries (the constitution) — bit-identical so the
            child's ``pinned_merkle_root()`` matches the parent's.
          * Top-``memory_working_cap`` non-pinned entries by ``weight``
            descending — the parent's "freshest / most-recently-retrieved"
            traces. Default cap is :attr:`DEFAULT_CHILD_WORKING_CAP`.

        Because the child's ``MemoryService`` reuses the parent's
        ``(dim, seed)`` rotation and ``_centroid``, every entry is copied
        verbatim (``bits_packed`` + ``l1`` are bit-identical), so a
        cosine query against the same vector returns the same top hit on
        parent and child.

        Real ERC-7715 session-key issuance is Slice 5C; here we
        materialise the child Python object plus its inherited memory so
        the demo step 6 can prove "tradable cognition" — the child can
        answer queries the parent already learned about, without paying
        Alice again.

        Args:
            child_budget_usdc: child's USDC budget (≤ parent's).
            extra_rules: additional constitution rules appended to the
                parent's set. The child's overall hash will differ from
                the parent's if any extras are provided.
            memory_working_cap: how many non-pinned entries to inherit.
                Defaults to :attr:`DEFAULT_CHILD_WORKING_CAP`. Pass 0
                to inherit ONLY the constitution (legacy behaviour).
        """
        if self.eoa is None:
            raise RuntimeError("Bob not bootstrapped")

        child_budget = (
            child_budget_usdc
            if child_budget_usdc is not None
            else max(0.0, self.budget_usdc / 2)
        )
        if child_budget > self.budget_usdc:
            raise ValueError(
                "child_budget cannot exceed parent budget — "
                "SUBDELEGATION_BOUND violation"
            )

        child_rules = list(self.constitution_rules) + list(extra_rules or [])
        child = Bob(
            budget_usdc=child_budget,
            constitution_rules=child_rules,
            embedding_model=self.embedding_model,
            embedding_dim=self.embedding_dim,
            # Reuse the parent's seed so the child's MemoryService rotation
            # matrix matches — that's what makes the verbatim entry copy
            # valid. ``bootstrap()`` constructs the child's MemoryService
            # with this seed; we then replace its empty entries dict with
            # the parent's cloned slice.
            seed=self.seed,
        )
        # bootstrap() will create the child's EOA, hash its (extended)
        # constitution, and build an EMPTY MemoryService seeded only with
        # the constitution rules. We then OVERWRITE that empty memory
        # with the parent's cloned state below.
        child.bootstrap()

        # Replace the freshly-built child memory with a deep clone of
        # the parent's. Doing this AFTER bootstrap() means the child has
        # a real EOA + constitution hash, and only the memory contents
        # are inherited.
        cap = (
            memory_working_cap
            if memory_working_cap is not None
            else self.DEFAULT_CHILD_WORKING_CAP
        )
        child.memory = self._clone_memory_for_child(working_cap=cap)
        # If the child's constitution differs from the parent's (extra
        # rules), pin the child's extra rules so their constitution
        # canonicalisation survives. ALL of the parent's pinned entries
        # are already in the cloned memory; only the brand-new extra
        # rules need pinning on the child side.
        for r in (extra_rules or []):
            text = _rule_canonical_text(r)
            vec = hash_to_vec(text, dim=self.embedding_dim, seed=self.seed)
            child.memory.add(
                trace_id=f"pinned:{r.get('rule_id', r['kind'])}",
                vec=vec,
                kind="pinned",
                pinned=True,
                payload={"text": text, **r},
            )
        return child

    def _clone_memory_for_child(self, *, working_cap: int) -> MemoryService:
        """Deep-clone this Bob's MemoryService for an outgoing child.

        Pinned entries are ALL copied (the constitution must transfer
        intact — that's the F10 / spec §6 contract). Non-pinned entries
        are filtered to the top ``working_cap`` by ``weight`` descending
        — newer / more-recently-retrieved entries first.

        Entries are copied byte-for-byte via the dataclass ``_Entry``
        constructor — ``bits_packed`` (and the ``l1`` scalar) are passed
        through with ``.copy()`` so the child can mutate its weights
        (decay, eviction) without disturbing the parent.

        Returns the new MemoryService. The caller assigns it to
        ``child.memory``.
        """
        if self.memory is None:
            raise RuntimeError("Bob not bootstrapped — no memory to clone")
        parent_mem = self.memory

        # Construct child MemoryService with parent's seed so rotation
        # matrices match. The decay_lambdas dict is copied so child
        # decay schedules are independent of parent's.
        child_mem = MemoryService(
            dim=parent_mem.dim,
            decay_lambdas=dict(parent_mem.decay_lambdas),
            seed=parent_mem.seed,
        )
        # Set the centroid BEFORE any add. We assign directly because
        # ``set_centroid`` would raise if there were existing entries;
        # there aren't, but doing this manually skips an unnecessary
        # validation hop and makes the intent explicit.
        if parent_mem._centroid is not None:
            child_mem._centroid = parent_mem._centroid.copy()
        # else: leave as None — child will arm zero on first add (if any).

        # Pinned entries — copy ALL of them verbatim.
        for tid, e in parent_mem.entries.items():
            if e.pinned:
                child_mem.entries[tid] = _Entry(
                    trace_id=e.trace_id,
                    kind=e.kind,
                    pinned=True,
                    payload=dict(e.payload),
                    bits_packed=e.bits_packed.copy(),
                    l1=e.l1,
                    norm=e.norm,
                    weight=e.weight,
                    last_decay_ts=e.last_decay_ts,
                )

        # Working entries — sort by weight DESCENDING, take top N.
        # ``weight`` proxies relevance: recently-added or recently-promoted
        # entries have weight near 1.0; long-decayed entries trend toward
        # the eviction threshold. This is the mandate's "top-K most-recent
        # + most-frequently-retrieved" surface, no separate retrieval
        # counter required.
        working = [e for e in parent_mem.entries.values() if not e.pinned]
        # Sort: weight desc, then last_decay_ts desc (recency tie-break),
        # then trace_id asc (deterministic final tie-break).
        working.sort(
            key=lambda e: (-e.weight, -e.last_decay_ts, e.trace_id),
        )
        if working_cap > 0:
            for e in working[:working_cap]:
                child_mem.entries[e.trace_id] = _Entry(
                    trace_id=e.trace_id,
                    kind=e.kind,
                    pinned=False,
                    payload=dict(e.payload),
                    bits_packed=e.bits_packed.copy(),
                    l1=e.l1,
                    norm=e.norm,
                    weight=e.weight,
                    last_decay_ts=e.last_decay_ts,
                )

        return child_mem


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _rule_canonical_text(rule: dict) -> str:
    kind = rule.get("kind", "CUSTOM")
    params = {k: v for k, v in rule.items() if k not in {"kind", "rule_id"}}
    kv = ",".join(f"{k}={params[k]}" for k in sorted(params))
    return f"constitution rule {kind} ({kv})"


def _parse_trace_text(text: str) -> dict:
    """Parse a templated trade-reasoning string.

    Format: ``"{side} {token} on {venue} size {N} USDC because {signal} risk {risk} conviction 0.{conv}"``.
    Returns a best-effort dict; missing fields default to safe values.
    """
    out: dict[str, Any] = {}
    if not text:
        return out
    parts = text.split()
    try:
        out["side"] = parts[0]
        out["token"] = parts[1]
        if "on" in parts:
            i = parts.index("on")
            out["venue"] = parts[i + 1]
        if "size" in parts:
            i = parts.index("size")
            out["size_usdc"] = float(parts[i + 1])
        if "risk" in parts:
            i = parts.index("risk")
            out["risk"] = parts[i + 1]
    except (IndexError, ValueError):
        pass
    return out


def _classify_intent(
    *,
    constitution_rules: list[dict],
    target: str,
    value: int,
    inner_selector: bytes,
    amount_units: int,
) -> str:
    """Predict which rule (if any) Slice 2's hook will trip on this TradeIntent.

    The classification is best-effort — the source of truth is always the
    on-chain hook. Used by the orchestrator to label evidence dicts.
    """
    for r in constitution_rules:
        kind = r.get("kind")
        if kind == "MAX_TRADE_SIZE":
            cap = _usdc_to_units(r.get("max_usdc", 0.0))
            if value > cap:
                return "MAX_TRADE_SIZE"
            if inner_selector == ERC20_TRANSFER_SELECTOR and amount_units > cap:
                return "MAX_TRADE_SIZE"
        elif kind == "VENUE_BLACKLIST":
            venues = {v.lower() for v in r.get("venues", [])}
            if target.lower() in venues:
                return "VENUE_BLACKLIST"
        elif kind == "NO_UNAUDITED_CONTRACTS":
            whitelist = [v.lower() for v in r.get("whitelist", [])]
            if whitelist and target.lower() not in whitelist:
                return "NO_UNAUDITED_CONTRACTS"
    return "OK"
