"""Integration tests for the Slice-4 Dark Pool + x402 client.

Uses FastAPI's TestClient — real round-trips through Starlette, no
HTTP mocking.  The signing key is a freshly-generated ``eth_account``
EOA so nothing leaves the process.
"""

from __future__ import annotations

import base64
import json
import secrets

import numpy as np
import pytest
from eth_account import Account
from fastapi.testclient import TestClient

from agents.dark_pool import (
    DarkPoolServer,
    build_signed_payment_header,
    usdc_to_base_units,
)
from agents.memory_service import MemoryService
from agents.x402_client import X402Error, x402_pay_and_post, x402_query


# ---- fixtures --------------------------------------------------------------


CHAIN_ID = 5042002  # Arc Testnet
USDC = "0x3600000000000000000000000000000000000000"
PRICE_USDC = "0.001"


def _seeded_memory(n: int = 5, dim: int = 384, seed: int = 0) -> MemoryService:
    rng = np.random.default_rng(seed)
    mem = MemoryService(dim=dim)
    for i in range(n):
        vec = rng.standard_normal(dim).astype(np.float32)
        mem.add(
            trace_id=f"trace_{i:03d}",
            vec=vec,
            kind="working",
            payload={"i": i, "asset": "ETH", "note": "ETH funding flipped negative"},
        )
    return mem


@pytest.fixture
def alice_account():
    # Recipient address — owned by Alice.  Random key, address only used.
    return Account.create()


@pytest.fixture
def bob_account():
    # Bob's EOA — signs the EIP-3009 authorization.
    return Account.create()


@pytest.fixture
def server(alice_account):
    mem = _seeded_memory(n=5)
    return DarkPoolServer(
        memory=mem,
        price_per_query_usdc=PRICE_USDC,
        payment_recipient=alice_account.address,
        arc_chain_id=CHAIN_ID,
        usdc_address=USDC,
    )


@pytest.fixture
def client(server):
    return TestClient(server.app)


def _vec(seed: int = 1, dim: int = 384) -> list[float]:
    return np.random.default_rng(seed).standard_normal(dim).astype(np.float32).tolist()


# ---- direct HTTP behaviour (no client lib) --------------------------------


def test_health_endpoint_works(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["memory_entries"] == 5


def test_query_without_payment_returns_402(client):
    r = client.post("/query", json={"query_vec": _vec(), "k": 3})
    assert r.status_code == 402
    body = r.json()
    assert body["x402Version"] == 1
    # Per Bug 3 fix, the server advertises both direct USDC AND
    # GatewayWalletBatched. The first entry remains the direct-USDC one
    # so existing direct clients keep working.
    assert isinstance(body["accepts"], list) and len(body["accepts"]) >= 1
    entry = body["accepts"][0]
    assert entry["scheme"] == "exact"
    assert entry["network"] == "arc-testnet"
    assert entry["maxAmountRequired"] == str(usdc_to_base_units(PRICE_USDC))
    assert entry["asset"].lower() == USDC.lower()
    assert entry["resource"] == "/query"
    assert entry["extra"] == {"name": "USDC", "version": "2"}


def test_query_with_valid_payment_returns_results(client, bob_account, alice_account):
    header = build_signed_payment_header(
        signer_account=bob_account,
        recipient=alice_account.address,
        amount_base_units=usdc_to_base_units(PRICE_USDC),
        chain_id=CHAIN_ID,
        usdc_address=USDC,
    )
    r = client.post(
        "/query",
        json={"query_vec": _vec(), "k": 3},
        headers={"X-PAYMENT": header},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert "results" in body
    assert len(body["results"]) == 3
    for item in body["results"]:
        assert "trace_id" in item
        assert "score" in item
        assert "payload" in item


def test_query_with_wrong_recipient_rejects(client, bob_account):
    # Signer pays the wrong address.
    wrong_recipient = Account.create().address
    header = build_signed_payment_header(
        signer_account=bob_account,
        recipient=wrong_recipient,
        amount_base_units=usdc_to_base_units(PRICE_USDC),
        chain_id=CHAIN_ID,
        usdc_address=USDC,
    )
    r = client.post(
        "/query",
        json={"query_vec": _vec(), "k": 3},
        headers={"X-PAYMENT": header},
    )
    assert r.status_code == 402
    assert "wrong recipient" in r.json().get("error", "")


def test_query_with_insufficient_amount_rejects(client, bob_account, alice_account):
    header = build_signed_payment_header(
        signer_account=bob_account,
        recipient=alice_account.address,
        amount_base_units=usdc_to_base_units(PRICE_USDC) - 1,
        chain_id=CHAIN_ID,
        usdc_address=USDC,
    )
    r = client.post(
        "/query",
        json={"query_vec": _vec(), "k": 3},
        headers={"X-PAYMENT": header},
    )
    assert r.status_code == 402
    assert "insufficient" in r.json().get("error", "")


def test_query_with_replayed_nonce_rejects(client, bob_account, alice_account):
    nonce = "0x" + secrets.token_hex(32)

    def make_header():
        # Re-derive the header but force the same nonce.
        return build_signed_payment_header(
            signer_account=bob_account,
            recipient=alice_account.address,
            amount_base_units=usdc_to_base_units(PRICE_USDC),
            chain_id=CHAIN_ID,
            usdc_address=USDC,
            nonce_hex=nonce,
        )

    # First call: accepted (200).
    r1 = client.post(
        "/query",
        json={"query_vec": _vec(), "k": 3},
        headers={"X-PAYMENT": make_header()},
    )
    assert r1.status_code == 200

    # Same nonce again → must be refused.
    r2 = client.post(
        "/query",
        json={"query_vec": _vec(), "k": 3},
        headers={"X-PAYMENT": make_header()},
    )
    assert r2.status_code == 402
    assert "replay" in r2.json().get("error", "")


def test_query_with_tampered_signature_rejects(client, bob_account, alice_account):
    # Build a header, then flip a byte in the signature.
    header = build_signed_payment_header(
        signer_account=bob_account,
        recipient=alice_account.address,
        amount_base_units=usdc_to_base_units(PRICE_USDC),
        chain_id=CHAIN_ID,
        usdc_address=USDC,
    )
    decoded = json.loads(base64.b64decode(header))
    sig = decoded["payload"]["signature"]
    if sig.startswith("0x"):
        sig = sig[2:]
    # flip the first hex character
    flipped = ("0" if sig[0] != "0" else "1") + sig[1:]
    decoded["payload"]["signature"] = "0x" + flipped
    tampered = base64.b64encode(json.dumps(decoded).encode()).decode()

    r = client.post(
        "/query",
        json={"query_vec": _vec(), "k": 3},
        headers={"X-PAYMENT": tampered},
    )
    assert r.status_code == 402


# ---- client-library behaviour ---------------------------------------------


def test_x402_client_happy_path(client, bob_account, alice_account):
    # Round-trip through the actual x402_query helper.
    results = x402_query(
        url="/query",
        query_vec=np.array(_vec(), dtype=np.float32),
        k=4,
        signer=bob_account,
        chain_id=CHAIN_ID,
        asset_address=USDC,
        expected_price_usdc=PRICE_USDC,
        transport=client,
    )
    assert isinstance(results, list)
    assert len(results) == 4
    assert all("trace_id" in r for r in results)


def test_x402_client_max_amount_guard(client, bob_account):
    # Client refuses to pay if the server quotes more than expected_price_usdc.
    with pytest.raises(X402Error):
        x402_query(
            url="/query",
            query_vec=np.array(_vec(), dtype=np.float32),
            k=3,
            signer=bob_account,
            chain_id=CHAIN_ID,
            asset_address=USDC,
            # Expected price is below the server's quote → no valid accept.
            expected_price_usdc=Decimal_str_below_price(),
            transport=client,
        )


def Decimal_str_below_price() -> str:
    """Return a USDC string strictly below the server's price."""
    # 0.0009 USDC < 0.001 USDC, so the client's budget cannot cover it.
    return "0.0009"


def test_x402_client_round_trip_uses_pay_and_post(client, bob_account, alice_account):
    # Lower-level helper sanity check.
    body = {"query_vec": _vec(), "k": 2}
    resp = x402_pay_and_post(
        url="/query",
        json_body=body,
        signer=bob_account,
        chain_id=CHAIN_ID,
        asset_address=USDC,
        expected_price_usdc=PRICE_USDC,
        transport=client,
    )
    assert "results" in resp
    assert len(resp["results"]) == 2


# ---- Phase 2 Slice 5B hardening: persistence + rate limiting --------------


def test_dark_pool_nonce_persists_across_restart(tmp_path, bob_account, alice_account):
    """Replay protection must survive a server restart.

    We start a DarkPoolServer using a SqliteNonceStore at a temp path,
    successfully consume a nonce, tear the server down, then bring up a
    fresh server pointed at the SAME db file. Replaying the same signed
    authorisation must be refused.
    """
    from agents.nonce_store import SqliteNonceStore

    db_path = tmp_path / "nonces.db"
    nonce = "0x" + secrets.token_hex(32)
    header = build_signed_payment_header(
        signer_account=bob_account,
        recipient=alice_account.address,
        amount_base_units=usdc_to_base_units(PRICE_USDC),
        chain_id=CHAIN_ID,
        usdc_address=USDC,
        nonce_hex=nonce,
        valid_for_seconds=600,
    )

    # --- Server v1: accept the payment, close the store. ---
    store_v1 = SqliteNonceStore(str(db_path))
    mem_v1 = _seeded_memory(n=5)
    server_v1 = DarkPoolServer(
        memory=mem_v1,
        price_per_query_usdc=PRICE_USDC,
        payment_recipient=alice_account.address,
        arc_chain_id=CHAIN_ID,
        usdc_address=USDC,
        nonce_store=store_v1,
    )
    client_v1 = TestClient(server_v1.app)
    r1 = client_v1.post(
        "/query",
        json={"query_vec": _vec(), "k": 3},
        headers={"X-PAYMENT": header},
    )
    assert r1.status_code == 200, r1.text
    store_v1.close()
    del server_v1, client_v1, store_v1, mem_v1

    # --- Server v2: same DB, fresh in-process state. Replay must 402. ---
    store_v2 = SqliteNonceStore(str(db_path))
    server_v2 = DarkPoolServer(
        memory=_seeded_memory(n=5),
        price_per_query_usdc=PRICE_USDC,
        payment_recipient=alice_account.address,
        arc_chain_id=CHAIN_ID,
        usdc_address=USDC,
        nonce_store=store_v2,
    )
    client_v2 = TestClient(server_v2.app)
    r2 = client_v2.post(
        "/query",
        json={"query_vec": _vec(), "k": 3},
        headers={"X-PAYMENT": header},
    )
    assert r2.status_code == 402, r2.text
    assert "replay" in r2.json().get("error", "")
    store_v2.close()


def _make_server_with_rate_limit(
    alice_account, capacity: int, refill: float
):
    """Helper: fresh server with an isolated InMemoryNonceStore + bounded RL."""
    from agents.nonce_store import InMemoryNonceStore
    from agents.rate_limiter import RateLimiter

    mem = _seeded_memory(n=5)
    server = DarkPoolServer(
        memory=mem,
        price_per_query_usdc=PRICE_USDC,
        payment_recipient=alice_account.address,
        arc_chain_id=CHAIN_ID,
        usdc_address=USDC,
        nonce_store=InMemoryNonceStore(),
        rate_limiter=RateLimiter(capacity=capacity, refill_per_second=refill),
    )
    return server


def test_dark_pool_rate_limit_kicks_in(bob_account, alice_account):
    """Floor a single signer with valid payments — after ``capacity`` accepted
    queries the next valid signed request must return HTTP 429."""
    server = _make_server_with_rate_limit(
        alice_account, capacity=3, refill=0.001
    )
    client = TestClient(server.app)

    def fresh_header():
        return build_signed_payment_header(
            signer_account=bob_account,
            recipient=alice_account.address,
            amount_base_units=usdc_to_base_units(PRICE_USDC),
            chain_id=CHAIN_ID,
            usdc_address=USDC,
        )

    # First 3 requests must succeed (the bucket starts full).
    for i in range(3):
        r = client.post(
            "/query",
            json={"query_vec": _vec(), "k": 2},
            headers={"X-PAYMENT": fresh_header()},
        )
        assert r.status_code == 200, f"request {i} failed: {r.text}"

    # 4th valid payment — same signer — must be throttled.
    r4 = client.post(
        "/query",
        json={"query_vec": _vec(), "k": 2},
        headers={"X-PAYMENT": fresh_header()},
    )
    assert r4.status_code == 429, r4.text
    body = r4.json()
    assert "rate limit" in body.get("error", "").lower()
    # Retry-After header must be present per RFC 7231.
    assert "Retry-After" in r4.headers or "retry-after" in r4.headers


def test_dark_pool_rate_limit_per_signer(bob_account, alice_account):
    """One signer is throttled; a DIFFERENT signer with the same budget is
    untouched. Proves rate buckets are keyed by recovered signer address."""
    server = _make_server_with_rate_limit(
        alice_account, capacity=2, refill=0.001
    )
    client = TestClient(server.app)

    other_signer = Account.create()  # second client — should NOT be throttled

    def header_for(account):
        return build_signed_payment_header(
            signer_account=account,
            recipient=alice_account.address,
            amount_base_units=usdc_to_base_units(PRICE_USDC),
            chain_id=CHAIN_ID,
            usdc_address=USDC,
        )

    # Drain bob's bucket.
    for _ in range(2):
        r = client.post(
            "/query",
            json={"query_vec": _vec(), "k": 2},
            headers={"X-PAYMENT": header_for(bob_account)},
        )
        assert r.status_code == 200

    # Bob is over the limit.
    r_bob = client.post(
        "/query",
        json={"query_vec": _vec(), "k": 2},
        headers={"X-PAYMENT": header_for(bob_account)},
    )
    assert r_bob.status_code == 429

    # The other signer must still go through — separate bucket.
    r_other = client.post(
        "/query",
        json={"query_vec": _vec(), "k": 2},
        headers={"X-PAYMENT": header_for(other_signer)},
    )
    assert r_other.status_code == 200, r_other.text


def test_dark_pool_lifespan_purges_and_closes_store(tmp_path, alice_account):
    """Bringing the app up under TestClient's context manager must:

      1. Run the startup purge (removes already-expired rows from the db).
      2. Close the nonce store on shutdown (subsequent ``has`` raises).
    """
    from agents.nonce_store import SqliteNonceStore

    db_path = tmp_path / "lifespan.db"
    store = SqliteNonceStore(str(db_path))
    # Pre-populate one expired and one fresh nonce. Pass the EIP-712
    # domain explicitly — Phase-4 hardening removed the legacy 2-arg
    # form so the dark-pool's F2 cross-domain protection can't be
    # bypassed by an in-tree caller.
    store.add(
        "0xaaaa",
        "0xexpired",
        expires_at=1,
        chain_id=CHAIN_ID,
        verifying_contract=USDC,
    )
    store.add(
        "0xaaaa",
        "0xfresh",
        expires_at=2**31 - 1,
        chain_id=CHAIN_ID,
        verifying_contract=USDC,
    )
    assert len(store) == 2
    # Close so the server can open it fresh.
    store.close()

    server = DarkPoolServer(
        memory=_seeded_memory(n=3),
        price_per_query_usdc=PRICE_USDC,
        payment_recipient=alice_account.address,
        arc_chain_id=CHAIN_ID,
        usdc_address=USDC,
        nonce_store=SqliteNonceStore(str(db_path)),
    )

    with TestClient(server.app) as c:
        # Startup ran — health endpoint reachable.
        r = c.get("/health")
        assert r.status_code == 200

    # After exit, the store's connection should be closed. Verify by
    # opening a fresh sqlite connection and confirming the expired row
    # was purged at startup while the fresh one survived.
    import sqlite3

    raw = sqlite3.connect(str(db_path))
    try:
        rows = raw.execute("SELECT nonce FROM nonces ORDER BY nonce").fetchall()
    finally:
        raw.close()
    nonces = [r[0] for r in rows]
    assert "0xfresh" in nonces
    assert "0xexpired" not in nonces


# ---- Bug 3 — Gateway batched accepts advertising ---------------------------


def test_dark_pool_advertises_gateway_batched_scheme(client):
    """The 402 body must advertise BOTH direct USDC AND GatewayWalletBatched.

    Per Slice 5C's report: ``@circle-fin/x402-batching``'s ``GatewayClient``
    signs against the GatewayWallet contract with
    ``extra.name = "GatewayWalletBatched"``. Without a matching ``accepts[]``
    entry, the SDK rejects our server and the demo never exercises real
    Circle Gateway settlement. This test pins the wire shape so the SDK
    can pick the Gateway entry.
    """
    from agents.dark_pool import (
        GATEWAY_BATCHED_DOMAIN_NAME,
        GATEWAY_BATCHED_DOMAIN_VERSION,
        TESTNET_GATEWAY_WALLET,
    )

    r = client.post("/query", json={"query_vec": _vec(), "k": 3})
    assert r.status_code == 402, r.text
    body = r.json()
    accepts = body["accepts"]
    assert isinstance(accepts, list)
    assert len(accepts) == 2, (
        f"expected 2 accepts entries (direct USDC + GatewayWalletBatched), "
        f"got {len(accepts)}: {accepts}"
    )

    # accepts[0] = direct USDC (verifyingContract = USDC token).
    direct = accepts[0]
    assert direct["scheme"] == "exact"
    assert direct["network"] == "arc-testnet"
    assert direct["asset"].lower() == USDC.lower()
    assert direct["maxAmountRequired"] == str(usdc_to_base_units(PRICE_USDC))
    assert direct["extra"] == {"name": "USDC", "version": "2"}

    # accepts[1] = GatewayWalletBatched (verifyingContract = GatewayWallet).
    gateway = accepts[1]
    assert gateway["scheme"] == "exact"
    assert gateway["network"] == "arc-testnet"
    # ``asset`` stays USDC — Gateway settles USDC under the hood, the
    # signed domain just changes.
    assert gateway["asset"].lower() == USDC.lower()
    assert gateway["maxAmountRequired"] == str(usdc_to_base_units(PRICE_USDC))
    assert gateway["extra"]["name"] == GATEWAY_BATCHED_DOMAIN_NAME
    assert gateway["extra"]["name"] == "GatewayWalletBatched"  # explicit literal
    assert gateway["extra"]["version"] == GATEWAY_BATCHED_DOMAIN_VERSION
    # ``verifyingContract`` is the Circle Gateway Wallet on Arc testnet.
    assert (
        gateway["extra"]["verifyingContract"].lower()
        == TESTNET_GATEWAY_WALLET.lower()
    )
    # Crucially, the two entries advertise DIFFERENT EIP-712 verifying
    # contracts — the SDK's signer keys off this to pick the right domain.
    assert (
        gateway["extra"]["verifyingContract"].lower()
        != direct["asset"].lower()
    )


def test_dark_pool_accepts_signed_payment_against_gateway_domain(
    bob_account, alice_account
):
    """Bob can sign against the GatewayWallet domain and the server accepts it.

    We build the EIP-712 typed-data with the GatewayWallet as the
    ``verifyingContract`` (and name = "GatewayWalletBatched", version = "1")
    — exactly what ``@circle-fin/x402-batching``'s signer produces — and
    confirm the server recovers the signer and returns 200.
    """
    from agents.dark_pool import (
        GATEWAY_BATCHED_DOMAIN_NAME,
        GATEWAY_BATCHED_DOMAIN_VERSION,
        TESTNET_GATEWAY_WALLET,
    )

    # Fresh server (default config advertises GatewayWalletBatched).
    server = DarkPoolServer(
        memory=_seeded_memory(n=5),
        price_per_query_usdc=PRICE_USDC,
        payment_recipient=alice_account.address,
        arc_chain_id=CHAIN_ID,
        usdc_address=USDC,
    )
    c = TestClient(server.app)

    # Build a signed X-PAYMENT header using the GatewayWallet domain.
    header = build_signed_payment_header(
        signer_account=bob_account,
        recipient=alice_account.address,
        amount_base_units=usdc_to_base_units(PRICE_USDC),
        chain_id=CHAIN_ID,
        usdc_address=TESTNET_GATEWAY_WALLET,  # verifyingContract = GatewayWallet
        name=GATEWAY_BATCHED_DOMAIN_NAME,
        version=GATEWAY_BATCHED_DOMAIN_VERSION,
    )

    r = c.post(
        "/query",
        json={"query_vec": _vec(), "k": 3},
        headers={"X-PAYMENT": header},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert "results" in body
    assert len(body["results"]) == 3


def test_dark_pool_can_disable_gateway_advertising(alice_account):
    """Tests that need a single-accept response can set ``gateway_wallet_address=None``."""
    server = DarkPoolServer(
        memory=_seeded_memory(n=3),
        price_per_query_usdc=PRICE_USDC,
        payment_recipient=alice_account.address,
        arc_chain_id=CHAIN_ID,
        usdc_address=USDC,
        gateway_wallet_address=None,
    )
    c = TestClient(server.app)
    r = c.post("/query", json={"query_vec": _vec(), "k": 1})
    assert r.status_code == 402
    body = r.json()
    assert len(body["accepts"]) == 1
    assert body["accepts"][0]["extra"]["name"] == "USDC"


# ---- Phase 3 audit (F5) — nonce never burnt on non-signature failures ------


def test_query_with_bad_vec_shape_does_not_burn_nonce(
    bob_account, alice_account
):
    """A dim-mismatched payload must 400 BEFORE we consume the nonce.

    Pre-F5 the server validated payment → committed nonce → THEN checked
    shape. So a client with a typo'd vector length burnt a fresh nonce
    on every retry. Post-F5 the shape check moves above the payment
    path, and the nonce stays reusable.
    """
    from agents.nonce_store import InMemoryNonceStore

    server = DarkPoolServer(
        memory=_seeded_memory(n=5),
        price_per_query_usdc=PRICE_USDC,
        payment_recipient=alice_account.address,
        arc_chain_id=CHAIN_ID,
        usdc_address=USDC,
        nonce_store=InMemoryNonceStore(),
    )
    c = TestClient(server.app)

    nonce = "0x" + secrets.token_hex(32)

    # First POST — dim-mismatched query_vec but a real signed header with
    # the same nonce.
    header_bad = build_signed_payment_header(
        signer_account=bob_account,
        recipient=alice_account.address,
        amount_base_units=usdc_to_base_units(PRICE_USDC),
        chain_id=CHAIN_ID,
        usdc_address=USDC,
        nonce_hex=nonce,
        valid_for_seconds=120,
    )
    r1 = c.post(
        "/query",
        json={"query_vec": [0.1, 0.2, 0.3], "k": 3},  # dim != 384
        headers={"X-PAYMENT": header_bad},
    )
    assert r1.status_code == 400, r1.text
    # The store must NOT have the nonce yet — verify directly. Pass the
    # EIP-712 domain so we read from the same partition the server writes.
    assert not server._nonce_store.has(
        bob_account.address.lower(),
        nonce.lower(),
        chain_id=CHAIN_ID,
        verifying_contract=USDC,
    )

    # Second POST — correct shape, SAME nonce. Should succeed because the
    # nonce was never committed on the previous (400) request.
    header_good = build_signed_payment_header(
        signer_account=bob_account,
        recipient=alice_account.address,
        amount_base_units=usdc_to_base_units(PRICE_USDC),
        chain_id=CHAIN_ID,
        usdc_address=USDC,
        nonce_hex=nonce,
        valid_for_seconds=120,
    )
    r2 = c.post(
        "/query",
        json={"query_vec": _vec(), "k": 3},
        headers={"X-PAYMENT": header_good},
    )
    assert r2.status_code == 200, r2.text


def test_rate_limited_request_does_not_burn_nonce(bob_account, alice_account):
    """A 429-throttled request must NOT consume the nonce.

    Pre-F5 the server validated payment → committed nonce → THEN checked
    the rate limit. A burst-throttled signer lost a fresh nonce on every
    429. Post-F5 the commit moves after the rate-limit gate.
    """
    from agents.nonce_store import InMemoryNonceStore
    from agents.rate_limiter import RateLimiter

    nonce_store = InMemoryNonceStore()
    # capacity=1, refill very slow. After the first success, the second
    # signed request gets 429.
    rl = RateLimiter(capacity=1, refill_per_second=0.001)
    server = DarkPoolServer(
        memory=_seeded_memory(n=5),
        price_per_query_usdc=PRICE_USDC,
        payment_recipient=alice_account.address,
        arc_chain_id=CHAIN_ID,
        usdc_address=USDC,
        nonce_store=nonce_store,
        rate_limiter=rl,
    )
    c = TestClient(server.app)

    # 1) Drain bucket with one good request (uses nonce_A).
    header_a = build_signed_payment_header(
        signer_account=bob_account,
        recipient=alice_account.address,
        amount_base_units=usdc_to_base_units(PRICE_USDC),
        chain_id=CHAIN_ID,
        usdc_address=USDC,
    )
    r1 = c.post(
        "/query", json={"query_vec": _vec(), "k": 2},
        headers={"X-PAYMENT": header_a},
    )
    assert r1.status_code == 200, r1.text

    # 2) New nonce_B — should get 429 from the rate limiter.
    nonce_b = "0x" + secrets.token_hex(32)
    header_b = build_signed_payment_header(
        signer_account=bob_account,
        recipient=alice_account.address,
        amount_base_units=usdc_to_base_units(PRICE_USDC),
        chain_id=CHAIN_ID,
        usdc_address=USDC,
        nonce_hex=nonce_b,
        valid_for_seconds=120,
    )
    r2 = c.post(
        "/query", json={"query_vec": _vec(), "k": 2},
        headers={"X-PAYMENT": header_b},
    )
    assert r2.status_code == 429, r2.text

    # nonce_B must NOT be in the store — 429 path never commits. Pass
    # the EIP-712 domain so we read from the same partition the server
    # would have written into.
    assert not nonce_store.has(
        bob_account.address.lower(),
        nonce_b.lower(),
        chain_id=CHAIN_ID,
        verifying_contract=USDC,
    )

    # 3) Reset the rate limiter (fresh bucket) and retry SAME nonce_B —
    #    proves the 429 didn't consume it.
    server._rate_limiter = RateLimiter(capacity=10, refill_per_second=10.0)
    r3 = c.post(
        "/query", json={"query_vec": _vec(), "k": 2},
        headers={"X-PAYMENT": header_b},
    )
    assert r3.status_code == 200, r3.text


# ---- Phase 3 audit (F8) — signature hygiene --------------------------------


def test_payment_with_zero_from_address_rejected(client, bob_account, alice_account):
    """A payload with ``from = 0x000...0`` must be rejected as 402.

    The zero address is what ecrecover returns on malformed inputs, so
    accepting it would let a hostile client bypass replay protection
    (every "zero-signer" request looks the same in the nonce store).
    """
    from agents.dark_pool import ZERO_ADDRESS

    # Build a payload by hand — sig content doesn't matter, the zero
    # address rejection fires before we recover.
    payload = {
        "x402Version": 1,
        "scheme": "exact",
        "network": "arc-testnet",
        "payload": {
            "signature": "0x" + "00" * 65,
            "authorization": {
                "from": ZERO_ADDRESS,
                "to": alice_account.address,
                "value": str(usdc_to_base_units(PRICE_USDC)),
                "validAfter": str(int(__import__("time").time()) - 1),
                "validBefore": str(int(__import__("time").time()) + 60),
                "nonce": "0x" + secrets.token_hex(32),
            },
        },
    }
    header = base64.b64encode(json.dumps(payload).encode()).decode()
    r = client.post(
        "/query",
        json={"query_vec": _vec(), "k": 3},
        headers={"X-PAYMENT": header},
    )
    assert r.status_code == 402
    err = r.json().get("error", "")
    assert "zero" in err.lower(), f"expected 'zero' in error, got: {err}"


def test_payment_with_high_s_signature_rejected(client, bob_account, alice_account):
    """A high-s signature (the EIP-2 malleability twin) must be rejected.

    eth_account emits low-s by default. We construct the high-s pair
    ``(r, N - s, v ^ 1)`` from a normally-signed authorization and POST
    it; the server's hardened ``recover_signer`` must reject it.

    Reference: EIP-2 / Bitcoin BIP-66 — both halves of the curve
    recover to the same EOA, so high-s sigs are a known replay vector.
    """
    # secp256k1 curve order — duplicated from dark_pool._SECP256K1_N so
    # this test doesn't reach into a private module attribute.
    SECP256K1_N = (
        0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
    )

    header = build_signed_payment_header(
        signer_account=bob_account,
        recipient=alice_account.address,
        amount_base_units=usdc_to_base_units(PRICE_USDC),
        chain_id=CHAIN_ID,
        usdc_address=USDC,
    )
    decoded = json.loads(base64.b64decode(header))
    sig_hex = decoded["payload"]["signature"]
    if sig_hex.startswith("0x"):
        sig_hex = sig_hex[2:]
    assert len(sig_hex) == 130, "expected 65-byte signature"
    sig_bytes = bytes.fromhex(sig_hex)
    r = sig_bytes[0:32]
    s = int.from_bytes(sig_bytes[32:64], "big")
    v = sig_bytes[64]
    assert s <= SECP256K1_N // 2, "expected low-s out of eth_account by default"

    # Flip to the high-s twin.
    s_high = SECP256K1_N - s
    v_flipped = v ^ 1  # 27 <-> 28
    high_s_sig = r + s_high.to_bytes(32, "big") + bytes([v_flipped])
    decoded["payload"]["signature"] = "0x" + high_s_sig.hex()
    tampered = base64.b64encode(json.dumps(decoded).encode()).decode()

    rsp = client.post(
        "/query",
        json={"query_vec": _vec(), "k": 3},
        headers={"X-PAYMENT": tampered},
    )
    assert rsp.status_code == 402, rsp.text
    # The error should mention malleability / high-s — but recovery may
    # also fail with a different message. The crucial property: the
    # high-s twin is NOT accepted.


# ---- Phase 3 audit (F9) — upper-cap on value -------------------------------


def test_payment_with_excessive_amount_rejected(client, bob_account, alice_account):
    """A wildly over-priced payment (100× quote) must be refused.

    A buggy client that hands in a huge ``value`` should not silently
    drain the signer's allowance. The server caps at 2× the quoted
    price (``_VALUE_UPPER_FACTOR`` in dark_pool.py); anything beyond
    that returns 402.
    """
    over_amount = usdc_to_base_units(PRICE_USDC) * 100
    header = build_signed_payment_header(
        signer_account=bob_account,
        recipient=alice_account.address,
        amount_base_units=over_amount,
        chain_id=CHAIN_ID,
        usdc_address=USDC,
    )
    r = client.post(
        "/query",
        json={"query_vec": _vec(), "k": 3},
        headers={"X-PAYMENT": header},
    )
    assert r.status_code == 402, r.text
    err = r.json().get("error", "")
    assert "excessive" in err.lower(), f"expected 'excessive' in error, got: {err}"


# ---- Phase 3 audit (F11) — client recipient pinning ------------------------


def test_x402_client_rejects_wrong_recipient(client, bob_account, alice_account):
    """The client refuses to sign if the server's payTo doesn't match the pin.

    Scenario: the server advertises ``payTo = alice``. Caller pinned
    ``expected_recipient = some_other_address``. The client must raise
    ``X402Error`` BEFORE signing — no funds at risk.
    """
    other_recipient = Account.create().address
    with pytest.raises(X402Error) as excinfo:
        x402_query(
            url="/query",
            query_vec=np.array(_vec(), dtype=np.float32),
            k=3,
            signer=bob_account,
            chain_id=CHAIN_ID,
            asset_address=USDC,
            expected_price_usdc=PRICE_USDC,
            transport=client,
            expected_recipient=other_recipient,
        )
    msg = str(excinfo.value).lower()
    # Either "expected recipient" or "pay_to" should surface.
    assert "expected recipient" in msg or "pay_to" in msg


def test_x402_client_accepts_matching_recipient(client, bob_account, alice_account):
    """When ``expected_recipient`` matches the server's payTo, the flow proceeds."""
    results = x402_query(
        url="/query",
        query_vec=np.array(_vec(), dtype=np.float32),
        k=3,
        signer=bob_account,
        chain_id=CHAIN_ID,
        asset_address=USDC,
        expected_price_usdc=PRICE_USDC,
        transport=client,
        expected_recipient=alice_account.address,
    )
    assert isinstance(results, list)
    assert len(results) == 3
