"""Dark Pool server — paywalled MemoryService over x402.

Slice 4 / §4.6 of `docs/superpowers/specs/2026-05-21-constrained-cognition-design.md`.

The server wraps a Slice-1 ``MemoryService`` instance and exposes a single
endpoint, ``POST /query``, that returns the top-k matches for a 384-d query
vector.  Access is gated by the x402 HTTP-402 protocol (https://x402.org):

  1. Client POSTs ``{"query_vec": [...], "k": 10}``.
  2. Server returns ``HTTP 402`` with an ``accepts`` array describing the
     EIP-3009 ``TransferWithAuthorization`` it will accept.
  3. Client signs the typed-data with its EOA, base64-encodes the JSON
     {scheme, network, payload} blob, and retries with the ``X-PAYMENT``
     header set.
  4. Server validates the signature via ``ecrecover``, checks the recipient,
     amount, asset, validity window, and nonce, and returns the query
     results.

For the hackathon scope the server **does not actually settle the payment
on chain** — it validates the EIP-712 signature, records the nonce, and
returns the data.  Off-chain settlement happens in a separate Circle
Gateway batching step, out-of-scope for this slice.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import os
import secrets
import threading
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import numpy as np
from eth_account import Account
from eth_account.messages import encode_typed_data
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from agents.memory_service import MemoryService
from agents.nonce_store import NonceStore, SqliteNonceStore
from agents.rate_limiter import RateLimiter


logger = logging.getLogger(__name__)


# Default sqlite path for the persistent nonce store. Override with the
# ``DARKPOOL_NONCE_DB`` env var.
DEFAULT_NONCE_DB_PATH = "/tmp/darkpool_nonces.db"
DEFAULT_RATE_CAPACITY = 60
DEFAULT_RATE_REFILL_PER_SECOND = 1.0
# Purge expired nonces from the store once per minute by default.
_PURGE_INTERVAL_SECONDS = 60.0


# --- Constants --------------------------------------------------------------

USDC_DECIMALS = 6
X402_VERSION = 1
DEFAULT_SCHEME = "exact"
DEFAULT_NETWORK = "arc-testnet"

# Phase 3 audit (F8) — secp256k1 curve order. We reject high-s signatures
# (s > N/2) to prevent EIP-2 / ECDSA-malleability replay where (r, s, v)
# and (r, N-s, v^1) recover to the same address but produce a distinct
# 65-byte payload. eth_account's `sign_message` emits low-s by default,
# but a hostile client can post-process the signature to flip it to the
# high-s twin — we reject those.
_SECP256K1_N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
_SECP256K1_HALF_N = _SECP256K1_N // 2

# Phase 3 audit (F8) — explicit zero-address rejection.
ZERO_ADDRESS = "0x" + "0" * 40

# Phase 3 audit (F9) — upper bound on `value`. The 402 advertises an
# `maxAmountRequired` of `self.price_units`; we accept up to 2× that to
# tolerate small rounding/fee adjustments by a well-behaved client, but
# reject blatantly excessive transfers so a compromised or malicious
# client cannot accidentally drain a gas-station EOA.
_VALUE_UPPER_FACTOR = 2

# --- Circle Gateway batched scheme -----------------------------------------
#
# Mirrors the constants in ``@circle-fin/x402-batching@3.0.4`` (verified by
# reading ``node_modules/@circle-fin/x402-batching/dist/server/index.mjs``):
#
#     CIRCLE_BATCHING_NAME    = "GatewayWalletBatched"
#     CIRCLE_BATCHING_VERSION = "1"
#     CIRCLE_BATCHING_SCHEME  = "exact"                  (same as direct USDC)
#     TESTNET_GATEWAY_WALLET  = 0x0077777d7EBA4688BDeF3E311b846F25870A19B9
#     MAINNET_GATEWAY_WALLET  = 0x77777777Dcc4d5A8B6E418Fd04D8997ef11000eE
#
# Phase 2 / Bug 3 fix: the dark pool advertises BOTH a direct-USDC entry
# (EIP-712 verifyingContract = USDC) and a GatewayWalletBatched entry
# (EIP-712 verifyingContract = GatewayWallet) so that
# ``@circle-fin/x402-batching``'s ``GatewayClient.pay()`` can find a
# matching scheme. Without the second entry the SDK rejects our server
# because its signer fixes ``extra.name = "GatewayWalletBatched"`` and
# refuses to fall back to direct USDC.
GATEWAY_BATCHED_DOMAIN_NAME = "GatewayWalletBatched"
GATEWAY_BATCHED_DOMAIN_VERSION = "1"
TESTNET_GATEWAY_WALLET = "0x0077777d7EBA4688BDeF3E311b846F25870A19B9"
MAINNET_GATEWAY_WALLET = "0x77777777Dcc4d5A8B6E418Fd04D8997ef11000eE"


def usdc_to_base_units(amount: float | str | Decimal) -> int:
    """Convert a human USDC amount to 6-decimal base units (atomic)."""
    return int((Decimal(str(amount)) * (10**USDC_DECIMALS)).to_integral_value())


def base_units_to_usdc(units: int) -> Decimal:
    return Decimal(units) / (10**USDC_DECIMALS)


# --- EIP-712 / EIP-3009 typed data ------------------------------------------

# EIP-3009 TransferWithAuthorization typed data.  The Circle-issued USDC
# contract uses ``name = "USDC"`` and ``version = "2"`` per
# `use-usdc.md` / `use-gateway.md`.
_TRANSFER_WITH_AUTH_TYPES = {
    "EIP712Domain": [
        {"name": "name", "type": "string"},
        {"name": "version", "type": "string"},
        {"name": "chainId", "type": "uint256"},
        {"name": "verifyingContract", "type": "address"},
    ],
    "TransferWithAuthorization": [
        {"name": "from", "type": "address"},
        {"name": "to", "type": "address"},
        {"name": "value", "type": "uint256"},
        {"name": "validAfter", "type": "uint256"},
        {"name": "validBefore", "type": "uint256"},
        {"name": "nonce", "type": "bytes32"},
    ],
}


def build_typed_data(
    *,
    from_addr: str,
    to_addr: str,
    value: int,
    valid_after: int,
    valid_before: int,
    nonce_hex: str,
    chain_id: int,
    verifying_contract: str,
    name: str = "USDC",
    version: str = "2",
) -> dict[str, Any]:
    """Build the EIP-712 typed-data dict for a TransferWithAuthorization."""
    if not nonce_hex.startswith("0x"):
        nonce_hex = "0x" + nonce_hex
    return {
        "types": _TRANSFER_WITH_AUTH_TYPES,
        "domain": {
            "name": name,
            "version": version,
            "chainId": int(chain_id),
            "verifyingContract": verifying_contract,
        },
        "primaryType": "TransferWithAuthorization",
        "message": {
            "from": from_addr,
            "to": to_addr,
            "value": int(value),
            "validAfter": int(valid_after),
            "validBefore": int(valid_before),
            "nonce": nonce_hex,
        },
    }


def recover_signer(typed_data: dict[str, Any], signature_hex: str) -> str:
    """Run ecrecover on an EIP-712 typed-data + 65-byte signature.

    Phase 3 audit (F8) hardening:
      * signature MUST be exactly 65 bytes (130 hex chars after stripping 0x).
      * neither ``r`` nor ``s`` may be zero (would short-circuit ecrecover).
      * ``s`` MUST be in the lower half of the secp256k1 order (EIP-2 low-s);
        high-s signatures are the malleability twin of a valid sig and are
        rejected to avoid one-bit-flip replay.
      * the recovered address MUST NOT be the zero address (ecrecover returns
        ``0x000...0`` on inputs that don't match a curve point; some libs
        treat this as "success", we treat it as failure).
    """
    if signature_hex.startswith("0x") or signature_hex.startswith("0X"):
        sig_hex_raw = signature_hex[2:]
    else:
        sig_hex_raw = signature_hex
    if len(sig_hex_raw) != 130:
        raise ValueError(
            f"signature must be 65 bytes (130 hex chars), got {len(sig_hex_raw)}"
        )
    try:
        sig_bytes = bytes.fromhex(sig_hex_raw)
    except ValueError as exc:
        raise ValueError(f"signature is not valid hex: {exc}") from exc
    r = int.from_bytes(sig_bytes[0:32], "big")
    s = int.from_bytes(sig_bytes[32:64], "big")
    if r == 0:
        raise ValueError("signature r component is zero")
    if s == 0:
        raise ValueError("signature s component is zero")
    if s > _SECP256K1_HALF_N:
        raise ValueError("signature has high-s (EIP-2 malleability)")

    msg = encode_typed_data(full_message=typed_data)
    recovered = Account.recover_message(msg, signature="0x" + sig_hex_raw)
    if recovered.lower() == ZERO_ADDRESS:
        raise ValueError("ecrecover returned zero address")
    return recovered


# --- Request / response schemas ---------------------------------------------


class QueryBody(BaseModel):
    """Client request body for /query."""

    query_vec: list[float] = Field(..., description="Pre-computed 384-d embedding.")
    k: int = Field(10, ge=1, le=100, description="Top-k.")


class AddBody(BaseModel):
    """Client request body for /add.

    Phase 5 Stream D (B12) — agents push successful reasoning traces back
    into the shared dark pool index, making the "hive-mind" claim from
    docs/research.md actually true (parent agents WRITE, not just READ).

    ``trace_id`` MUST be globally unique within the pool — the server
    rejects duplicates rather than silently overwriting, so two agents
    that paid for the same slot don't lose data.

    ``vec`` MUST match the pool's dimensionality (typically 384 for
    MiniLM). A mismatch returns 400 BEFORE the nonce is consumed — the
    F5 nonce-burn fix is preserved on this surface too.

    ``payload`` is an arbitrary JSON object stored alongside the vector
    (e.g. ``{"text": "...", "side": "long", ...}``). The server makes no
    schema demands beyond JSON-serialisability.

    ``pinned`` is intentionally NOT exposed on this endpoint: paid adds
    write working entries that decay normally. The pinned slot is
    reserved for the pool operator's constitution rules.
    """

    trace_id: str = Field(..., min_length=1, max_length=256)
    vec: list[float] = Field(..., description="Pre-computed embedding.")
    payload: dict[str, Any] = Field(
        default_factory=dict,
        description="Free-form JSON payload stored with the entry.",
    )
    kind: str = Field(
        default="working",
        description="One of working/episodic/semantic.",
    )


# --- Server -----------------------------------------------------------------


@dataclass
class _PaymentRequirements:
    """The single ``accepts`` entry returned in a 402."""

    scheme: str
    network: str
    max_amount_required: str   # USDC base units, as string
    resource: str
    description: str
    mime_type: str
    pay_to: str
    max_timeout_seconds: int
    asset: str
    extra: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "scheme": self.scheme,
            "network": self.network,
            "maxAmountRequired": self.max_amount_required,
            "resource": self.resource,
            "description": self.description,
            "mimeType": self.mime_type,
            "payTo": self.pay_to,
            "maxTimeoutSeconds": self.max_timeout_seconds,
            "asset": self.asset,
            "extra": self.extra,
        }


class DarkPoolServer:
    """FastAPI app wrapping a MemoryService behind an x402 paywall."""

    def __init__(
        self,
        memory: MemoryService,
        *,
        price_per_query_usdc: float | str | Decimal = "0.001",
        # Phase 5 Stream D (B12) — paid POST /add. Defaults to the same
        # price as /query so the demo's hive-mind claim works without an
        # extra config knob. Operators that want a different add price
        # can set this independently. NOTE: the current ``_validate_payment``
        # uses ``self.price_units`` for the amount check, so for B12 we
        # constrain ``price_per_add_usdc`` to match ``price_per_query_usdc``
        # in practice — a future refactor can split the two paths.
        price_per_add_usdc: float | str | Decimal | None = None,
        payment_recipient: str,
        arc_chain_id: int = 5042002,
        usdc_address: str = "0x3600000000000000000000000000000000000000",
        network: str = DEFAULT_NETWORK,
        usdc_name: str = "USDC",
        usdc_version: str = "2",
        max_timeout_seconds: int = 60,
        nonce_store: NonceStore | None = None,
        rate_limiter: RateLimiter | None = None,
        # Phase 2 / Bug 3 — advertise GatewayWalletBatched alongside direct
        # USDC. Set to ``None`` to disable Gateway batched advertising (used
        # by tests that want a single-accept response). Default is Arc
        # testnet's Gateway Wallet address.
        gateway_wallet_address: str | None = TESTNET_GATEWAY_WALLET,
        gateway_domain_name: str = GATEWAY_BATCHED_DOMAIN_NAME,
        gateway_domain_version: str = GATEWAY_BATCHED_DOMAIN_VERSION,
    ) -> None:
        self.memory = memory
        self.price_units = usdc_to_base_units(price_per_query_usdc)
        # Phase 5 Stream D (B12) — /add price. ``None`` means "same as
        # /query". We materialise it as a separate field so the route
        # handler can advertise the right ``maxAmountRequired`` without
        # the existing ``_validate_payment`` flow changing shape.
        self.add_price_units = (
            usdc_to_base_units(price_per_add_usdc)
            if price_per_add_usdc is not None
            else self.price_units
        )
        if self.add_price_units != self.price_units:
            # We share ``_validate_payment`` between /query and /add, and
            # that helper hard-codes ``self.price_units`` for the amount
            # gate. Until that helper is split, the two prices must match
            # or paid adds will be rejected as "insufficient amount".
            # See B12 implementation notes in this file's docstring.
            raise NotImplementedError(
                "split /query and /add prices not yet supported; "
                "set price_per_add_usdc == price_per_query_usdc"
            )
        self.payment_recipient = payment_recipient
        self.chain_id = int(arc_chain_id)
        self.usdc_address = usdc_address
        self.network = network
        self.usdc_name = usdc_name
        self.usdc_version = usdc_version
        self.max_timeout_seconds = int(max_timeout_seconds)
        self.gateway_wallet_address = gateway_wallet_address
        self.gateway_domain_name = gateway_domain_name
        self.gateway_domain_version = gateway_domain_version

        # Persistent replay protection. Defaults to a sqlite-backed store at
        # ``$DARKPOOL_NONCE_DB`` (or ``/tmp/darkpool_nonces.db``) so that
        # nonces survive a server restart. Tests inject ``InMemoryNonceStore``
        # to keep test isolation cheap.
        if nonce_store is None:
            path = os.environ.get("DARKPOOL_NONCE_DB", DEFAULT_NONCE_DB_PATH)
            nonce_store = SqliteNonceStore(path)
        self._nonce_store: NonceStore = nonce_store
        self._nonce_lock = threading.Lock()

        # Per-signer token-bucket throttle. Defaults match the hackathon
        # demo profile (60 queries burst, 1 q/s sustained).
        if rate_limiter is None:
            rate_limiter = RateLimiter(
                capacity=DEFAULT_RATE_CAPACITY,
                refill_per_second=DEFAULT_RATE_REFILL_PER_SECOND,
            )
        self._rate_limiter: RateLimiter = rate_limiter

        # Background purge task handle — owned by the uvicorn lifespan.
        self._purge_task: asyncio.Task[None] | None = None

        self.app = FastAPI(
            title="AgoraHack Dark Pool",
            lifespan=self._lifespan,
        )
        self._register_routes()

    # ------------------------------------------------------------------
    # Lifespan — purge expired nonces + close store on shutdown.
    # ------------------------------------------------------------------

    @contextlib.asynccontextmanager
    async def _lifespan(self, _app: FastAPI):  # noqa: ANN202
        # Eager purge at startup so we don't carry over expired rows
        # from a previous run.
        try:
            self._nonce_store.purge_expired(int(time.time()))
        except Exception:  # noqa: BLE001
            logger.exception("startup nonce purge failed")

        self._purge_task = asyncio.create_task(
            self._purge_loop(), name="darkpool-nonce-purge"
        )
        try:
            yield
        finally:
            if self._purge_task is not None:
                self._purge_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await self._purge_task
                self._purge_task = None
            try:
                self._nonce_store.close()
            except Exception:  # noqa: BLE001
                logger.exception("nonce store close failed")

    async def _purge_loop(self) -> None:
        """Purge expired nonces every ``_PURGE_INTERVAL_SECONDS`` seconds."""
        try:
            while True:
                await asyncio.sleep(_PURGE_INTERVAL_SECONDS)
                try:
                    self._nonce_store.purge_expired(int(time.time()))
                except Exception:  # noqa: BLE001
                    logger.exception("nonce purge cycle failed")
        except asyncio.CancelledError:
            raise

    # ------------------------------------------------------------------
    # FastAPI wiring
    # ------------------------------------------------------------------

    def _register_routes(self) -> None:
        @self.app.get("/health")
        async def health() -> dict[str, Any]:
            return {
                "ok": True,
                "memory_entries": len(self.memory),
                "price_units": self.price_units,
                "add_price_units": self.add_price_units,
                "recipient": self.payment_recipient,
                "asset": self.usdc_address,
                "chain_id": self.chain_id,
            }

        @self.app.post("/query")
        async def query_route(request: Request) -> Any:
            return await self._handle_query(request)

        # Phase 5 Stream D (B12) — agents push successful reasoning
        # traces back into the shared index. The "hive-mind dark pool"
        # claim from docs/research.md requires a write path; this is it.
        @self.app.post("/add")
        async def add_route(request: Request) -> Any:
            return await self._handle_add(request)

    # ------------------------------------------------------------------
    # 402 helpers
    # ------------------------------------------------------------------

    def _payment_requirements(self, resource: str) -> _PaymentRequirements:
        """Direct-USDC accepts entry (verifyingContract = USDC token)."""
        return _PaymentRequirements(
            scheme=DEFAULT_SCHEME,
            network=self.network,
            max_amount_required=str(self.price_units),
            resource=resource,
            description="RaBitQ dark pool query",
            mime_type="application/json",
            pay_to=self.payment_recipient,
            max_timeout_seconds=self.max_timeout_seconds,
            asset=self.usdc_address,
            extra={"name": self.usdc_name, "version": self.usdc_version},
        )

    def _gateway_payment_requirements(
        self, resource: str
    ) -> _PaymentRequirements | None:
        """Circle GatewayWalletBatched accepts entry, or ``None`` if disabled.

        The Gateway-batched scheme keeps ``scheme="exact"`` and ``asset =
        USDC`` so a client filter on those still matches, but the EIP-712
        ``verifyingContract`` is the GatewayWallet contract and the domain
        ``name``/``version`` change. The ``@circle-fin/x402-batching`` SDK
        requires exactly this shape — without it the SDK refuses to sign.
        """
        if self.gateway_wallet_address is None:
            return None
        return _PaymentRequirements(
            scheme=DEFAULT_SCHEME,
            network=self.network,
            max_amount_required=str(self.price_units),
            resource=resource,
            description="RaBitQ dark pool query (Circle Gateway batched)",
            mime_type="application/json",
            pay_to=self.payment_recipient,
            max_timeout_seconds=self.max_timeout_seconds,
            asset=self.usdc_address,
            extra={
                "name": self.gateway_domain_name,
                "version": self.gateway_domain_version,
                "verifyingContract": self.gateway_wallet_address,
            },
        )

    def _all_accepts(self, resource: str) -> list[dict[str, Any]]:
        """Build the ``accepts[]`` array in canonical order.

        ``accepts[0]`` = direct USDC EIP-3009 (verifyingContract = USDC).
        ``accepts[1]`` = Circle GatewayWalletBatched, if a Gateway wallet
        address is configured.
        """
        entries: list[dict[str, Any]] = [
            self._payment_requirements(resource).to_dict()
        ]
        gw = self._gateway_payment_requirements(resource)
        if gw is not None:
            entries.append(gw.to_dict())
        return entries

    def _make_402(self, resource: str, error: str | None = None) -> JSONResponse:
        body: dict[str, Any] = {
            "x402Version": X402_VERSION,
            "accepts": self._all_accepts(resource),
        }
        if error is not None:
            body["error"] = error
        return JSONResponse(status_code=402, content=body)

    # ------------------------------------------------------------------
    # Payment validation
    # ------------------------------------------------------------------

    def _parse_payment_header(self, raw: str) -> dict[str, Any]:
        """Decode the base64 X-PAYMENT header into its parsed payload."""
        try:
            decoded = base64.b64decode(raw).decode()
            obj = json.loads(decoded)
        except Exception as exc:  # noqa: BLE001
            raise ValueError(f"malformed X-PAYMENT header: {exc}") from exc
        if not isinstance(obj, dict):
            raise ValueError("X-PAYMENT must decode to a JSON object")
        return obj

    def _validate_payment(
        self, payment: dict[str, Any]
    ) -> tuple[
        bool, str | None, str | None, str | None, int | None, str | None
    ]:
        """Validate a parsed X-PAYMENT payload WITHOUT consuming the nonce.

        Phase 3 audit (F5): this helper now performs every check it used
        to, **except** writing the nonce to the persistent store. The
        nonce write moves into :meth:`_commit_nonce`, called by the
        request handler **after** rate-limit / dim-check pass — so a
        rejected request never burns the user's nonce.

        Phase 4 audit (B2 / P0-1): the return tuple now includes the
        ``verifying_contract`` that successfully recovered the signer.
        That value is the nonce-store partition key — without it, F2's
        cross-domain replay protection lands in the legacy sentinel
        domain (a bug, not a feature).

        Returns ``(ok, error_msg, signer_lc, nonce_lc, valid_before,
        verifying_contract)``. On failure, the trailing fields may be
        ``None``. The replay check (``self._nonce_store.has``) is still
        done here so a known replay returns 402 with ``"nonce replayed"``
        immediately — only the **insert** is deferred.
        """
        if payment.get("scheme") != DEFAULT_SCHEME:
            return (
                False,
                f"unsupported scheme: {payment.get('scheme')!r}",
                None,
                None,
                None,
                None,
            )
        if payment.get("network") != self.network:
            return (
                False,
                f"unsupported network: {payment.get('network')!r}",
                None,
                None,
                None,
                None,
            )

        payload = payment.get("payload")
        if not isinstance(payload, dict):
            return False, "missing payload", None, None, None, None

        auth = payload.get("authorization")
        sig = payload.get("signature")
        if not isinstance(auth, dict) or not isinstance(sig, str):
            return (
                False,
                "payload missing authorization or signature",
                None,
                None,
                None,
                None,
            )

        try:
            from_addr = auth["from"]
            to_addr = auth["to"]
            value = int(auth["value"])
            valid_after = int(auth["validAfter"])
            valid_before = int(auth["validBefore"])
            nonce_hex = auth["nonce"]
        except (KeyError, TypeError, ValueError) as exc:
            return (
                False,
                f"authorization fields invalid: {exc}",
                None,
                None,
                None,
                None,
            )

        # Phase 3 audit (F8): explicit zero-address rejection BEFORE we
        # try to recover the signer. ecrecover can synthesise the zero
        # address from garbage inputs; ``recover_signer`` already rejects
        # that, but we also reject ``from = 0x000...0`` up-front so a
        # malformed payload with hand-crafted sig fails the cheap check.
        if not isinstance(from_addr, str) or from_addr.lower() == ZERO_ADDRESS:
            return False, "from address is zero", None, None, None, None

        now = int(time.time())
        if valid_before <= now:
            return False, "authorization expired", None, None, None, None
        if valid_after > now + 5:  # small clock-skew buffer
            return (
                False,
                "authorization not yet valid",
                None,
                None,
                None,
                None,
            )

        # Recipient + asset + amount checks.
        if to_addr.lower() != self.payment_recipient.lower():
            return False, "wrong recipient", None, None, None, None
        if value < self.price_units:
            return (
                False,
                f"insufficient amount: got {value}, need {self.price_units}",
                None,
                None,
                None,
                None,
            )
        # Phase 3 audit (F9): upper-cap. Reject blatantly excessive
        # transfers (more than 2× the quoted price) so a buggy/malicious
        # client can't accidentally drain its allowance through this
        # endpoint.
        max_value = self.price_units * _VALUE_UPPER_FACTOR
        if value > max_value:
            return (
                False,
                f"excessive amount: got {value}, max {max_value}",
                None,
                None,
                None,
                None,
            )

        # Try the direct-USDC EIP-712 domain first (verifyingContract =
        # USDC). If recovery doesn't match, try the GatewayWalletBatched
        # domain (verifyingContract = GatewayWallet). The two domains
        # produce different EIP-712 digests for the same authorization,
        # so we must try each one explicitly.
        candidate_domains: list[tuple[str, str, str]] = [
            (self.usdc_address, self.usdc_name, self.usdc_version),
        ]
        if self.gateway_wallet_address is not None:
            candidate_domains.append(
                (
                    self.gateway_wallet_address,
                    self.gateway_domain_name,
                    self.gateway_domain_version,
                )
            )

        recovered: str | None = None
        # Phase 4 audit (B2 / P0-1) — track which verifyingContract
        # actually recovered the signer so we can namespace the nonce by
        # the matching domain (NOT the legacy sentinel).
        recovered_verifying_contract: str | None = None
        last_err: str | None = None
        for verifying_contract, name, version in candidate_domains:
            typed = build_typed_data(
                from_addr=from_addr,
                to_addr=to_addr,
                value=value,
                valid_after=valid_after,
                valid_before=valid_before,
                nonce_hex=nonce_hex,
                chain_id=self.chain_id,
                verifying_contract=verifying_contract,
                name=name,
                version=version,
            )
            try:
                candidate = recover_signer(typed, sig)
            except Exception as exc:  # noqa: BLE001
                last_err = f"signature recovery failed: {exc}"
                continue
            if candidate.lower() == from_addr.lower():
                recovered = candidate
                recovered_verifying_contract = verifying_contract
                break

        if recovered is None:
            return (
                False,
                last_err or "signature does not match from-address",
                None,
                None,
                None,
                None,
            )

        signer_lc = recovered.lower()
        nonce_lc = nonce_hex.lower()
        # Phase 4 audit (B2 / P0-1) — the (chain_id, verifying_contract)
        # pair is part of the replay-protection key now. The verifying
        # contract we recovered against tells us which EIP-712 domain
        # signed the auth; that domain's address is the nonce-store
        # partition key. Net effect: a signature replayed across chains
        # or across token contracts is detected as a fresh nonce, not
        # silently accepted.
        # Recovery succeeded — by construction ``recovered`` and
        # ``recovered_verifying_contract`` are both set above. Use the
        # actual domain that matched the signature.
        assert recovered_verifying_contract is not None
        domain_verifying_contract = recovered_verifying_contract
        # Replay protection — read-only check here. The actual
        # ``add()`` is deferred to ``_commit_nonce`` so that a 400 or
        # 429 farther down the handler doesn't burn a fresh nonce.
        # We still hold the lock briefly so a concurrent commit_nonce
        # can't slip a duplicate past us between has() and the return.
        with self._nonce_lock:
            if self._nonce_store.has(
                signer_lc,
                nonce_lc,
                chain_id=self.chain_id,
                verifying_contract=domain_verifying_contract,
            ):
                return (
                    False,
                    "nonce replayed",
                    signer_lc,
                    nonce_lc,
                    valid_before,
                    domain_verifying_contract,
                )

        return (
            True,
            None,
            signer_lc,
            nonce_lc,
            valid_before,
            domain_verifying_contract,
        )

    def _commit_nonce(
        self,
        signer_lc: str,
        nonce_lc: str,
        valid_before: int,
        *,
        verifying_contract: str,
    ) -> tuple[bool, str | None]:
        """Atomic check-and-insert for a (chain_id, verifying_contract, signer, nonce).

        Phase 3 audit (F5): callers MUST invoke this only after every
        other check has passed (rate-limit + dim-check). On the rare
        race where two concurrent requests with the same nonce both
        slip past the ``has()`` in ``_validate_payment``, the second
        one trips the duplicate-detection here and is rejected.

        Phase 4 audit (B2 / P0-1): ``chain_id`` and ``verifying_contract``
        are now required so the row lands in the proper namespaced
        domain (NOT the legacy sentinel). Without this, F2's cross-domain
        protection would be theatre.

        Returns ``(ok, err)``.
        """
        with self._nonce_lock:
            if self._nonce_store.has(
                signer_lc,
                nonce_lc,
                chain_id=self.chain_id,
                verifying_contract=verifying_contract,
            ):
                return False, "nonce replayed"
            self._nonce_store.add(
                signer_lc,
                nonce_lc,
                expires_at=valid_before,
                chain_id=self.chain_id,
                verifying_contract=verifying_contract,
            )
        return True, None

    # ------------------------------------------------------------------
    # Request handler
    # ------------------------------------------------------------------

    async def _handle_query(self, request: Request) -> Any:
        resource = str(request.url.path)

        # 1. Parse the JSON body so we can give an honest 402 even on the
        #    first call (must include the query so we can return matches in
        #    one round trip after the signed retry).
        try:
            body_json = await request.json()
        except Exception:
            return JSONResponse(
                status_code=400,
                content={"error": "request body must be JSON"},
            )

        try:
            body = QueryBody(**body_json)
        except Exception as exc:  # noqa: BLE001
            return JSONResponse(
                status_code=400,
                content={"error": f"invalid body: {exc}"},
            )

        # 2. Phase 3 audit (F5): validate query_vec dimensionality NOW —
        #    before any payment-related work. A dim-mismatched request
        #    must NOT burn a nonce or even reach the signature path.
        vec = np.asarray(body.query_vec, dtype=np.float32)
        if vec.shape != (self.memory.dim,):
            return JSONResponse(
                status_code=400,
                content={
                    "error": (
                        f"query_vec must be length {self.memory.dim}, "
                        f"got {vec.shape}"
                    )
                },
            )

        # 3. Did the client send a payment?
        payment_header = request.headers.get("X-PAYMENT")
        if not payment_header:
            return self._make_402(resource)

        # 4. Decode + validate (signature, recipient, amount, replay-check).
        #    The nonce is NOT yet inserted into the store — we defer that
        #    to step 7 so rate-limit / late failures don't burn a nonce.
        try:
            payment = self._parse_payment_header(payment_header)
        except ValueError as exc:
            return self._make_402(resource, error=str(exc))

        (
            ok,
            err,
            signer,
            nonce_lc,
            valid_before,
            verifying_contract,
        ) = self._validate_payment(payment)
        if not ok:
            return self._make_402(resource, error=err)

        # 5. Per-signer rate limit. The signature was good and the nonce
        #    wasn't a replay, but this signer has exceeded their burst
        #    capacity. Surface as HTTP 429 with a Retry-After header.
        #
        #    Phase 3 audit (F5): NOTE the nonce is still not committed
        #    here, so a 429'd request leaves the nonce reusable.
        assert signer is not None  # invariant after ok=True
        assert nonce_lc is not None
        assert valid_before is not None
        assert verifying_contract is not None
        # Phase 5 Stream D (B14): rate limiter API is now async-aware so we
        # don't park the event loop on a kernel mutex inside this handler.
        if not await self._rate_limiter.try_consume(signer):
            retry_after = await self._rate_limiter.retry_after(signer)
            # Round up to whole seconds for the header per RFC 7231.
            ra_int = max(1, int(retry_after) + (1 if retry_after % 1 else 0))
            return JSONResponse(
                status_code=429,
                content={
                    "error": "rate limit exceeded",
                    "signer": signer,
                    "retry_after_seconds": retry_after,
                },
                headers={"Retry-After": str(ra_int)},
            )

        # 6. Phase 3 audit (F5): NOW commit the nonce atomically. If a
        #    concurrent request raced past our earlier has() and inserted
        #    the same nonce in between, we surface that as 402 replayed.
        #
        # Phase 4 audit (B2 / P0-1): pass the EIP-712 domain so the nonce
        # lands in the namespaced partition (NOT the legacy sentinel).
        ok_commit, err_commit = self._commit_nonce(
            signer,
            nonce_lc,
            valid_before,
            verifying_contract=verifying_contract,
        )
        if not ok_commit:
            return self._make_402(resource, error=err_commit)

        # 7. Run the actual query.
        results = self.memory.query(vec, k=body.k)
        # MemoryService.query returns list[tuple[str, float]] per the canonical
        # spec (§4.1). We look up payload separately from the entries map so
        # we don't force MemoryService to widen its return type.
        out = []
        for tid, score in results:
            entry = self.memory.entries.get(tid)
            out.append({
                "trace_id": tid,
                "score": float(score),
                "payload": entry.payload if entry is not None else {},
            })
        return JSONResponse(status_code=200, content={"results": out})

    # ------------------------------------------------------------------
    # Phase 5 Stream D (B12) — paid POST /add: hive-mind write path.
    # ------------------------------------------------------------------

    async def _handle_add(self, request: Request) -> Any:
        """Paid write into the dark pool's MemoryService.

        Mirrors :meth:`_handle_query`'s validation order so the F5
        "don't burn the nonce on early failures" invariant holds on
        this path too:

          1. Parse JSON body. Bad JSON → 400.
          2. Validate ``AddBody`` shape. Missing fields → 400.
          3. Validate vector dimensionality. Wrong dim → 400 BEFORE
             nonce commit (B12 test d).
          4. Verify ``X-PAYMENT`` header. Missing → 402.
          5. Validate signature, recipient, amount, replay-check
             (read-only). Bad → 402.
          6. Per-signer rate limit. Over budget → 429.
          7. Commit nonce. Race-lost → 402.
          8. Insert into MemoryService.

        F11 (server-side recipient enforcement) is inherited from
        ``_validate_payment``'s ``to_addr.lower() != self.payment_recipient``
        check — a client signing for the wrong recipient is refused
        on /add for the same reason as /query.

        Pinned status is forced to False — paid adds write working
        entries that decay. Pinning is reserved for the operator.
        """
        resource = str(request.url.path)

        # 1. Parse JSON body.
        try:
            body_json = await request.json()
        except Exception:
            return JSONResponse(
                status_code=400,
                content={"error": "request body must be JSON"},
            )

        # 2. Validate AddBody shape.
        try:
            body = AddBody(**body_json)
        except Exception as exc:  # noqa: BLE001
            return JSONResponse(
                status_code=400,
                content={"error": f"invalid body: {exc}"},
            )

        # 3. Vector dimensionality check — BEFORE payment so a typo'd
        #    vector doesn't burn a fresh nonce (B12 test d / F5 invariant).
        vec = np.asarray(body.vec, dtype=np.float32)
        if vec.shape != (self.memory.dim,):
            return JSONResponse(
                status_code=400,
                content={
                    "error": (
                        f"vec must be length {self.memory.dim}, "
                        f"got {vec.shape}"
                    )
                },
            )

        # Kind must be a valid working-style kind. We refuse "pinned"
        # on this endpoint — pinned slots belong to the pool operator.
        if body.kind not in ("working", "episodic", "semantic"):
            return JSONResponse(
                status_code=400,
                content={
                    "error": (
                        f"kind must be one of working/episodic/semantic, "
                        f"got {body.kind!r}"
                    )
                },
            )

        # 4. Payment required.
        payment_header = request.headers.get("X-PAYMENT")
        if not payment_header:
            return self._make_402(resource)

        # 5. Decode + validate (signature, recipient, amount, replay-check).
        try:
            payment = self._parse_payment_header(payment_header)
        except ValueError as exc:
            return self._make_402(resource, error=str(exc))

        (
            ok,
            err,
            signer,
            nonce_lc,
            valid_before,
            verifying_contract,
        ) = self._validate_payment(payment)
        if not ok:
            return self._make_402(resource, error=err)

        assert signer is not None
        assert nonce_lc is not None
        assert valid_before is not None
        assert verifying_contract is not None

        # 6. Per-signer rate limit (async; no event-loop block per B14).
        if not await self._rate_limiter.try_consume(signer):
            retry_after = await self._rate_limiter.retry_after(signer)
            ra_int = max(1, int(retry_after) + (1 if retry_after % 1 else 0))
            return JSONResponse(
                status_code=429,
                content={
                    "error": "rate limit exceeded",
                    "signer": signer,
                    "retry_after_seconds": retry_after,
                },
                headers={"Retry-After": str(ra_int)},
            )

        # 7. Reject duplicate trace_id BEFORE we commit the nonce so a
        #    client can retry with a fresh trace_id and the same auth
        #    (per the F5 don't-burn-on-recoverable-failure invariant).
        if body.trace_id in self.memory.entries:
            return JSONResponse(
                status_code=409,
                content={
                    "error": f"trace_id already exists: {body.trace_id!r}",
                },
            )

        # 8. Commit nonce (atomic check-and-insert).
        ok_commit, err_commit = self._commit_nonce(
            signer,
            nonce_lc,
            valid_before,
            verifying_contract=verifying_contract,
        )
        if not ok_commit:
            return self._make_402(resource, error=err_commit)

        # 9. Insert into the MemoryService. Pinned forced to False —
        #    paid adds write working entries that decay normally.
        try:
            self.memory.add(
                trace_id=body.trace_id,
                vec=vec,
                kind=body.kind,
                pinned=False,
                payload=dict(body.payload),
            )
        except ValueError as exc:
            # The shape check above should catch dim mismatch; this
            # branch handles any future MemoryService validation
            # error (e.g. invalid kind) without leaking internals.
            logger.exception("memory.add raised for trace_id=%s", body.trace_id)
            return JSONResponse(
                status_code=400,
                content={"error": f"memory.add rejected entry: {exc}"},
            )

        return JSONResponse(
            status_code=200,
            content={
                "ok": True,
                "trace_id": body.trace_id,
                "memory_entries": len(self.memory),
                "signer": signer,
            },
        )


# --- Module-level app object for ``uvicorn agents.dark_pool:app`` -----------


class DarkPoolMisconfigured(RuntimeError):
    """Raised on startup if the operator forgot to set DARKPOOL_RECIPIENT.

    Phase 4 audit (B4 / P0-4): the previous build hard-coded a
    ``_DEFAULT_RECIPIENT = "0x0000000000000000000000000000000000000000"``
    fallback. An operator who forgot to set the env var would silently
    accept signed USDC transfers to the zero address — funds burned,
    looked like a working server. We now refuse to start.
    """


def _build_default_app() -> FastAPI:
    """Construct a default app for the uvicorn entrypoint.

    The real demo wires this up via Slice-5's orchestrator, but to keep
    ``uvicorn agents.dark_pool:app --port 8001`` runnable for ad-hoc
    smoke tests we provide a minimal default backed by a MemoryService
    loaded from ``$DARKPOOL_MEMORY_PATH`` (default ``/tmp/alice.mem``).

    Evaluated lazily — see ``__getattr__`` below — so importing this
    module does **not** require the memory file to exist. That used to
    crash on a fresh box (the file is created by ``agents/seed_alice.py``
    which itself imports agents code), so every consumer had to pre-touch
    a placeholder. Now imports are side-effect-free.

    Phase 4 audit (B4 / P0-4): refuse to start if ``DARKPOOL_RECIPIENT``
    is unset, empty, or equals the zero address. A server that accepts
    payments to ``0x0`` is a footgun: the signatures will validate, the
    nonce store will mark them as consumed, and the demo "succeeds" with
    burned funds. We surface the misconfiguration loudly at startup
    instead of letting the first paid query silently dispose of the
    user's USDC.
    """
    recipient = os.environ.get("DARKPOOL_RECIPIENT", "").strip()
    if not recipient or recipient.lower() == ZERO_ADDRESS:
        raise DarkPoolMisconfigured(
            "DARKPOOL_RECIPIENT is unset or equals the zero address — refusing "
            "to start. Set DARKPOOL_RECIPIENT to a non-zero EOA / SCA owned by "
            "the dark pool operator before launching `uvicorn agents.dark_pool:app`. "
            "Without this guard, every signed USDC transfer is directed at the "
            "burn address (Phase 4 audit B4 / P0-4)."
        )
    # Light sanity check: well-formed 0x-prefixed 20-byte address.
    if not (
        recipient.startswith("0x")
        and len(recipient) == 42
        and all(c in "0123456789abcdefABCDEF" for c in recipient[2:])
    ):
        raise DarkPoolMisconfigured(
            f"DARKPOOL_RECIPIENT {recipient!r} is not a valid 0x-prefixed "
            f"20-byte hex address. Refusing to start."
        )
    mem_path = os.environ.get("DARKPOOL_MEMORY_PATH", "/tmp/alice.mem")
    mem = MemoryService.load(mem_path)
    server = DarkPoolServer(
        memory=mem,
        price_per_query_usdc=os.environ.get("DARKPOOL_PRICE_USDC", "0.001"),
        payment_recipient=recipient,
        arc_chain_id=int(os.environ.get("DARKPOOL_CHAIN_ID", "5042002")),
        usdc_address=os.environ.get(
            "DARKPOOL_USDC_ADDRESS",
            "0x3600000000000000000000000000000000000000",
        ),
    )
    return server.app


# Cached lazy app. ``None`` until first ``agents.dark_pool.app`` access.
_DEFAULT_APP: FastAPI | None = None


def __getattr__(name: str) -> Any:
    """Lazy module attribute access — only build ``app`` when asked.

    ``uvicorn agents.dark_pool:app`` resolves ``app`` via attribute lookup,
    which triggers this hook and the eager load lands at runtime instead
    of import time. Code that imports this module for its types and
    helpers (alice, bob, orchestrator, tests) no longer pays for the
    memory file to exist.
    """
    if name == "app":
        global _DEFAULT_APP
        if _DEFAULT_APP is None:
            _DEFAULT_APP = _build_default_app()
        return _DEFAULT_APP
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# --- Test helper: build a fresh signed X-PAYMENT header ---------------------


def build_signed_payment_header(
    *,
    signer_account,
    recipient: str,
    amount_base_units: int,
    chain_id: int,
    usdc_address: str,
    network: str = DEFAULT_NETWORK,
    name: str = "USDC",
    version: str = "2",
    valid_for_seconds: int = 60,
    nonce_hex: str | None = None,
) -> str:
    """Produce a base64-encoded X-PAYMENT header value.

    Exposed publicly so tests (and the x402_client) can share the encoding
    logic without duplicating EIP-712 plumbing.
    """
    now = int(time.time())
    if nonce_hex is None:
        nonce_hex = "0x" + secrets.token_hex(32)
    if not nonce_hex.startswith("0x"):
        nonce_hex = "0x" + nonce_hex
    typed = build_typed_data(
        from_addr=signer_account.address,
        to_addr=recipient,
        value=int(amount_base_units),
        valid_after=now - 1,
        valid_before=now + int(valid_for_seconds),
        nonce_hex=nonce_hex,
        chain_id=chain_id,
        verifying_contract=usdc_address,
        name=name,
        version=version,
    )
    msg = encode_typed_data(full_message=typed)
    signed = signer_account.sign_message(msg)
    payload = {
        "x402Version": X402_VERSION,
        "scheme": DEFAULT_SCHEME,
        "network": network,
        "payload": {
            "signature": signed.signature.hex()
            if isinstance(signed.signature, (bytes, bytearray))
            else signed.signature,
            "authorization": {
                "from": signer_account.address,
                "to": recipient,
                "value": str(int(amount_base_units)),
                "validAfter": str(now - 1),
                "validBefore": str(now + int(valid_for_seconds)),
                "nonce": nonce_hex,
            },
        },
    }
    raw = json.dumps(payload).encode()
    return base64.b64encode(raw).decode()
