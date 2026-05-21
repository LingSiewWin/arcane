# Slice 4 — Dark Pool API + x402 client

This slice owns Alice's paywalled memory-query service (`agents/dark_pool.py`)
and Bob's payment-and-query helper (`agents/x402_client.py`).  It implements
the HTTP-402 dance described in §8 of
`docs/superpowers/specs/2026-05-21-constrained-cognition-design.md` and at
https://x402.org.

## What it does

- **`DarkPoolServer`** — FastAPI app that wraps a Slice-1 `MemoryService`
  instance and exposes `POST /query`.  Every call is gated by a signed
  EIP-3009 `TransferWithAuthorization` (USDC on Arc testnet).
- **`x402_query` / `x402_pay_and_post`** — client-side helpers that handle
  the 402 → sign → retry loop with any signer that exposes
  `.address` and `.sign_message(SignableMessage)` (i.e. an
  `eth_account.Account`).

## Install

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r agents/requirements-darkpool.txt
```

## Run the server

```bash
export DARKPOOL_RECIPIENT=0xAliceSCA...           # required for real use
export DARKPOOL_PRICE_USDC=0.001
export DARKPOOL_CHAIN_ID=5042002
export DARKPOOL_USDC_ADDRESS=0x3600000000000000000000000000000000000000
export DARKPOOL_MEMORY_PATH=/tmp/alice.mem
uvicorn agents.dark_pool:app --port 8001
```

For programmatic use:

```python
from agents.memory_service import MemoryService
from agents.dark_pool import DarkPoolServer

mem = MemoryService.load("/tmp/alice.mem")
server = DarkPoolServer(
    memory=mem,
    price_per_query_usdc="0.001",
    payment_recipient="0xAliceSCA...",
    arc_chain_id=5042002,
    usdc_address="0x3600000000000000000000000000000000000000",
)
# server.app is a FastAPI instance
```

## Query as a client

```python
import os, numpy as np
from eth_account import Account
from agents.x402_client import x402_query

bob_eoa = Account.from_key(os.environ["BOB_TURNKEY_EOA_PK"])
query_vec = np.random.default_rng(0).standard_normal(384).astype(np.float32)

results = x402_query(
    url="http://localhost:8001/query",
    query_vec=query_vec,
    k=10,
    signer=bob_eoa,
    chain_id=5042002,
    asset_address="0x3600000000000000000000000000000000000000",
    max_amount_usdc="0.001",
)
for r in results:
    print(r["score"], r["trace_id"], r["payload"])
```

The signer **must** be a raw EOA — Circle Gateway's x402 path uses
`ecrecover`, which is incompatible with ERC-1271 SCA signatures.  Bob's
funds live in his SCA, but signing is delegated to the Turnkey EOA via an
ERC-7715 session key (handled in Slice 5).

## Embedding model

**Slice 4 does not compute embeddings.**  Callers pass a pre-computed
384-d `float32` vector.  The expected model is
`sentence-transformers/all-MiniLM-L6-v2`, per
`docs/agent_rabitq_demo.md` §A.

## x402 protocol details

### First request (no payment)

```
POST /query
Content-Type: application/json
{"query_vec": [...384 floats...], "k": 10}
```

### Server response

```
HTTP/1.1 402 Payment Required
Content-Type: application/json

{
  "x402Version": 1,
  "accepts": [{
    "scheme": "exact",
    "network": "arc-testnet",
    "maxAmountRequired": "1000",
    "resource": "/query",
    "description": "RaBitQ dark pool query",
    "mimeType": "application/json",
    "payTo": "0xAlice...",
    "maxTimeoutSeconds": 60,
    "asset": "0x3600...",
    "extra": {"name": "USDC", "version": "2"}
  }]
}
```

### Client retry

The client builds an EIP-712 `TransferWithAuthorization` (EIP-3009) struct,
signs it with the EOA, and re-issues the POST with:

```
X-PAYMENT: <base64(JSON({x402Version, scheme, network, payload:{signature, authorization}}))>
```

### Server validates

`ecrecover(typed_data, signature)` must equal `authorization.from`;
`authorization.to` must equal `payTo`; `value >= maxAmountRequired`;
`validBefore > now`; nonce never seen before.  All pass → `200` with
results; any fail → another `402` with an `error` field.

## Tests

```bash
agents/.venv/bin/python -m pytest agents/tests/test_dark_pool.py -v
```

All 10 tests pass against FastAPI's in-process `TestClient` — no HTTP is
mocked.

## What is NOT done in this slice

- **No actual on-chain settlement.**  The signed authorization is
  validated and the nonce is recorded, but nothing is submitted to Arc.
  Off-chain batching + on-chain settlement via Circle Gateway
  (`@circle-fin/x402-batching`) is the orchestrator's job (Slice 5).
- **Nonce replay-protection does not survive a server restart.**  The
  set is in-memory.  Persisting requires either a sqlite/Postgres write
  on every accepted payment, or relying on Circle Gateway's nonce
  registry once batched settlement is wired up.
- **No rate limiting** — a malicious payer can blast valid signed
  payments and DoS the memory service.  Out of scope here; add a
  bucketed limiter at the orchestrator layer.
- **No `/health` payment** — the `/health` endpoint is intentionally
  free for demo introspection.
