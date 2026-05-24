"""x402 client — pay-and-query helper for the Dark Pool.

Implements the HTTP-402 dance from https://x402.org:

  1. POST the request body to the resource URL.
  2. On HTTP 402, parse the ``accepts`` array, pick a payment requirement
     that matches our policy (correct scheme + network + asset + amount
     under ``max_amount_usdc``).
  3. Sign an EIP-3009 ``TransferWithAuthorization`` for the requirement.
  4. Encode {x402Version, scheme, network, payload:{signature, authorization}}
     as base64 JSON, attach as ``X-PAYMENT`` header, retry the POST.
  5. Return the JSON body on 200.

This module is deliberately transport-agnostic: ``transport`` can be either
an ``httpx.Client`` for real network calls or a ``starlette.testclient
.TestClient`` / ``fastapi.testclient.TestClient`` for in-process tests.
"""

from __future__ import annotations

import base64
import json
import secrets
import time
from decimal import Decimal
from typing import Any, Optional

import httpx
import numpy as np
from eth_account.messages import encode_typed_data

from agents.dark_pool import (
    DEFAULT_NETWORK,
    DEFAULT_SCHEME,
    X402_VERSION,
    build_typed_data,
    usdc_to_base_units,
)


class X402Error(RuntimeError):
    """Raised when the x402 handshake cannot complete."""


def _normalise_accept(entry: dict[str, Any]) -> dict[str, Any]:
    """Pull the fields we care about out of a server ``accepts`` entry."""
    return {
        "scheme": entry.get("scheme"),
        "network": entry.get("network"),
        "max_amount_required": int(entry.get("maxAmountRequired", 0)),
        "pay_to": entry.get("payTo"),
        "asset": entry.get("asset"),
        "max_timeout_seconds": int(entry.get("maxTimeoutSeconds", 60)),
        "extra": entry.get("extra") or {},
        "resource": entry.get("resource"),
    }


def _pick_accept(
    accepts: list[dict[str, Any]],
    *,
    network: str,
    asset_address: str,
    expected_price_units: int,
    expected_recipient: Optional[str] = None,
) -> dict[str, Any]:
    """Pick the first accept entry that satisfies our policy.

    Phase 3 audit (F11): when ``expected_recipient`` is provided, the
    client refuses to sign for ANY entry whose ``payTo`` differs. This
    prevents a malicious or compromised server from swapping the
    recipient on us after we've already approved the price — Alice's
    public address is pinned by the caller, not the server.

    Phase 4 audit (B6 / P1 #8): we also enforce a **strict** price cap.
    The previous policy was "accept anything ≤ max_units", which let a
    server quote 1.5× the advertised price as long as the client's
    budget cap (a single round-trip override) was generous. The audit
    HIGH-2 finding asked for ``value == expected_price`` (strict). We
    pass ``expected_price_units`` and refuse any entry whose
    ``maxAmountRequired`` exceeds it — the upper bound is set by the
    caller from out-of-band knowledge (e.g. the price quoted on Alice's
    public landing page), NOT by the server's 402 body.
    """
    recipient_lc = expected_recipient.lower() if expected_recipient else None
    matched_amount_ok = False
    matched_recipient_ok = False
    for raw in accepts:
        a = _normalise_accept(raw)
        if a["scheme"] != DEFAULT_SCHEME:
            continue
        if a["network"] != network:
            continue
        if (a["asset"] or "").lower() != asset_address.lower():
            continue
        if a["max_amount_required"] > expected_price_units:
            continue  # server quoted more than we expected — refuse
        matched_amount_ok = True
        if recipient_lc is not None and (a["pay_to"] or "").lower() != recipient_lc:
            # Server says pay to X, caller pinned recipient Y. Refuse.
            continue
        matched_recipient_ok = True
        return a
    # Distinguish the two failure modes so callers can react usefully.
    if recipient_lc is not None and matched_amount_ok and not matched_recipient_ok:
        raise X402Error(
            f"server pay_to did not match expected recipient {expected_recipient}"
        )
    raise X402Error(
        f"no acceptable payment requirement: no entry matched "
        f"scheme/network/asset and stayed at or under expected price "
        f"{expected_price_units} base units"
    )


def _build_payment_header(
    *,
    signer,
    accept: dict[str, Any],
    chain_id: int,
    asset_address: str,
    network: str,
) -> str:
    name = accept["extra"].get("name", "USDC")
    version = accept["extra"].get("version", "2")
    now = int(time.time())
    valid_after = now - 1
    valid_before = now + int(accept["max_timeout_seconds"])
    nonce_hex = "0x" + secrets.token_hex(32)
    value = int(accept["max_amount_required"])

    typed = build_typed_data(
        from_addr=signer.address,
        to_addr=accept["pay_to"],
        value=value,
        valid_after=valid_after,
        valid_before=valid_before,
        nonce_hex=nonce_hex,
        chain_id=chain_id,
        verifying_contract=asset_address,
        name=name,
        version=version,
    )
    msg = encode_typed_data(full_message=typed)
    signed = signer.sign_message(msg)
    sig = signed.signature
    sig_hex = sig.hex() if isinstance(sig, (bytes, bytearray)) else sig

    payload = {
        "x402Version": X402_VERSION,
        "scheme": DEFAULT_SCHEME,
        "network": network,
        "payload": {
            "signature": sig_hex,
            "authorization": {
                "from": signer.address,
                "to": accept["pay_to"],
                "value": str(value),
                "validAfter": str(valid_after),
                "validBefore": str(valid_before),
                "nonce": nonce_hex,
            },
        },
    }
    return base64.b64encode(json.dumps(payload).encode()).decode()


def _post(transport, url: str, *, json_body: dict[str, Any], headers: dict[str, str]):
    """Tiny adapter so both httpx.Client and TestClient work."""
    return transport.post(url, json=json_body, headers=headers)


def x402_pay_and_post(
    *,
    url: str,
    json_body: dict[str, Any],
    signer,
    chain_id: int,
    asset_address: str,
    expected_price_usdc: float | str | Decimal,
    network: str = DEFAULT_NETWORK,
    transport=None,
    expected_recipient: Optional[str] = None,
) -> dict[str, Any]:
    """Perform the full x402 dance and return the decoded JSON response.

    ``signer`` must expose ``.address`` and ``.sign_message(SignableMessage)``
    — i.e. an ``eth_account.Account`` instance.

    Phase 3 audit (F11): pass ``expected_recipient`` to pin the
    server's ``payTo`` — if any 402 ``accepts[]`` entry advertises a
    different address, the client refuses to sign and raises
    ``X402Error``.

    Phase 4 audit (B6 / P1 #8): ``expected_price_usdc`` is now a
    **REQUIRED** argument and is treated as a strict upper bound on
    ``maxAmountRequired``. The previous ``max_amount_usdc`` parameter
    was a loose "budget cap" — useful for protecting the wallet, but
    NOT for catching a server that quoted 1.5× its advertised price on
    a single round trip. Callers MUST know the price ahead of time
    (typically from the operator's public landing page) and pass it
    here; if the server's quote exceeds it on any ``accepts[]`` entry,
    the client refuses to sign and raises ``X402Error``.

    The argument name changes deliberately so static analysis flags any
    in-tree caller still using the loose ``max_amount_usdc`` form.
    """
    expected_units = usdc_to_base_units(expected_price_usdc)
    close_after = False
    if transport is None:
        transport = httpx.Client(timeout=30.0)
        close_after = True

    try:
        # 1. First attempt — no payment.
        first = _post(transport, url, json_body=json_body, headers={})
        if first.status_code == 200:
            return first.json()
        if first.status_code != 402:
            raise X402Error(
                f"unexpected status {first.status_code}: {first.text[:200]}"
            )

        try:
            challenge = first.json()
        except Exception as exc:  # noqa: BLE001
            raise X402Error(f"402 body not JSON: {exc}") from exc

        accepts = challenge.get("accepts") or []
        if not accepts:
            raise X402Error("402 had no accepts array")

        accept = _pick_accept(
            accepts,
            network=network,
            asset_address=asset_address,
            expected_price_units=expected_units,
            expected_recipient=expected_recipient,
        )

        # 2. Sign + retry. The picker already enforced
        # ``max_amount_required <= expected_price_units``, so the
        # signed value cannot exceed what we approved.
        header = _build_payment_header(
            signer=signer,
            accept=accept,
            chain_id=chain_id,
            asset_address=asset_address,
            network=network,
        )
        second = _post(
            transport, url, json_body=json_body, headers={"X-PAYMENT": header}
        )
        if second.status_code != 200:
            raise X402Error(
                f"server still refused after payment: "
                f"{second.status_code} {second.text[:200]}"
            )
        return second.json()
    finally:
        if close_after:
            transport.close()


def x402_query(
    *,
    url: str,
    query_vec: np.ndarray,
    k: int,
    signer,
    chain_id: int,
    asset_address: str,
    expected_price_usdc: float | str | Decimal,
    network: str = DEFAULT_NETWORK,
    transport=None,
    expected_recipient: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Convenience wrapper for the Dark Pool ``/query`` endpoint.

    Returns the ``results`` list directly.

    The caller is responsible for computing ``query_vec`` (Slice-4 does NOT
    compute embeddings — see README).

    Phase 3 audit (F11) / Phase 4 audit (B6 / P1 #8): both
    ``expected_recipient`` and ``expected_price_usdc`` are forwarded so
    the wrapper inherits the same pinning guarantees as
    :func:`x402_pay_and_post`. ``expected_price_usdc`` is REQUIRED.
    """
    vec = np.asarray(query_vec, dtype=np.float32)
    body = {"query_vec": vec.tolist(), "k": int(k)}
    resp = x402_pay_and_post(
        url=url,
        json_body=body,
        signer=signer,
        chain_id=chain_id,
        asset_address=asset_address,
        expected_price_usdc=expected_price_usdc,
        network=network,
        transport=transport,
        expected_recipient=expected_recipient,
    )
    return resp.get("results", [])
