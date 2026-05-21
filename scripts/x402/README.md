# `scripts/x402/` — Bob's batched x402 client

Phase 2 / Slice 5C of the **Constrained Cognition** demo
(spec: `docs/superpowers/specs/2026-05-21-constrained-cognition-design.md` §8).

This module is the TypeScript half of the x402 payment loop. Bob's agent uses
this client to pay Alice's dark pool (`agents/dark_pool.py`) in USDC over the
HTTP-402 protocol, while accumulating signed authorizations for batched
settlement.

```
Bob's Turnkey EOA / local EOA
       │  (raw ecrecover signer — backed by Slice 3)
       ▼
X402BatchClient.pay(url, { body, maxAmountUsdc })
       │
       │   1. POST url
       │   2. ← HTTP 402 + accepts[]
       │   3. sign EIP-3009 TransferWithAuthorization
       │   4. POST url + X-PAYMENT
       │   5. ← HTTP 200 + results
       │   6. enqueue authorization (auto | manual | immediate)
       ▼
queue: PaymentAuthorization[]
       │
       │  flush() triggered by size, age, manual, or shutdown
       ▼
CircleGatewaySettler.settle(items)
   ├─ digest-only (no creds)  → returns BatchedSettlement, broadcast=false
   └─ real Gateway broadcast  → on-chain settle via @circle-fin/x402-batching
                                 (gated on GATEWAY_PRIVATE_KEY + deposited USDC)
```

## Files

| File | Role |
|---|---|
| `types.ts` | Shared types + error classes |
| `signer.ts` | Turnkey EOA → EIP-712/EIP-3009 signing adapter |
| `circle_gateway.ts` | `@circle-fin/x402-batching` wrapper + digest helper |
| `batch_client.ts` | `X402BatchClient` — main entry point |
| `bob_client.ts` | CLI used by Slice 5D's `demo_e2e.py` |
| `tests/batch_client.test.ts` | 13 vitest tests (7 real subprocess) |
| `tests/_darkpool_test_app.py` | Test-only ASGI module — seeded `DarkPoolServer` |

## Quick start

```bash
# 1. Install deps (already in package.json)
pnpm install

# 2. Run the tests
node node_modules/vitest/vitest.mjs run scripts/x402/tests
# 13 passed (4 unit + 7 subprocess + 2 lifecycle)

# 3. Spin up a dark pool, pay it
PYTHONPATH=. DARKPOOL_TEST_RECIPIENT=0x000...000a \
  agents/.venv/bin/uvicorn scripts.x402.tests._darkpool_test_app:app \
  --host 127.0.0.1 --port 8001 &

# 4. Generate a Bob spawn (Slice 3) and pay
pnpm run spawn-agent -- --name bob --budget 10 --expiry-min 5 \
  --constitution-hash 0x$(printf 'a%.0s' {1..64}) > /tmp/bob_spawn.json

# 5. Bob queries via the CLI
pnpm run bob-client -- query http://127.0.0.1:8001/query \
  --vec '[0,0,1,0,0,0,0,0]' \
  --max-usdc 0.001 \
  --from-spawn-result /tmp/bob_spawn.json \
  --mode auto \
  --flush-after
```

## API

### `X402BatchClient`

```typescript
import { X402BatchClient } from "./batch_client.js";

const client = new X402BatchClient({
  signer: turnkeyEoa,                                 // RawEoa with privateKey OR Turnkey
  chainId: 5042002,                                   // Arc testnet
  network: "arc-testnet",
  usdcAddress: "0x3600000000000000000000000000000000000000",
  gatewayBatchMode: "auto",                           // "auto" | "manual" | "immediate"
  maxBatchSize: 100,
  maxBatchAgeSeconds: 30,
  // Optional — enables real Circle Gateway broadcast on flush
  // gatewayPrivateKey: process.env.GATEWAY_PRIVATE_KEY,
  // gatewayChainName: "arcTestnet",
});

const result = await client.pay(`${baseUrl}/query`, {
  body: { query_vec: [...], k: 10 },
  maxAmountUsdc: "0.001",
});
// result.data                    — server's JSON body
// result.paymentAuthorization    — the signed EIP-3009 blob
// result.status                  — 200

// Drain the queue:
const settlement = await client.flush();
// settlement.items.length        — number of authorizations flushed
// settlement.totalValue          — total USDC base units
// settlement.broadcast           — whether we hit Circle Gateway
// settlement.txHash              — Arc settlement tx hash, if broadcast

// Cleanup:
await client.close();
```

### Modes

| Mode | Behavior |
|---|---|
| `immediate` | every `pay()` synchronously "settles" (digests) its own authorization. No queue. |
| `manual` | every `pay()` enqueues. Caller must invoke `client.flush()`. |
| `auto` | every `pay()` enqueues. Auto-flush fires when `queue.length >= maxBatchSize` OR oldest item ages past `maxBatchAgeSeconds`. |

## SDK integration — which path was taken

> **The user explicitly asked us to "pull circle skills" — we did.**
> Canonical docs read: `use-gateway.md`, `use-usdc.md`, `use-arc.md`,
> `use-developer-controlled-wallets.md` from
> `~/.arc-canteen/context/docs/circlefin-skills/`.

**`@circle-fin/x402-batching@3.0.4` exists on npm and is installed as a
dependency.** We use it for:

- `CHAIN_CONFIGS.arcTestnet` — canonical Arc testnet addresses + Gateway
  wallet contract.
- `GATEWAY_DOMAINS.arcTestnet === 26` — confirmed against `use-gateway.md`.
- `GATEWAY_AUTH_VALIDITY_WINDOW_SECONDS` — the SDK's recommended validity
  window (604,900 sec).
- `GatewayClient` — the buyer-side SDK that wraps deposit / pay / withdraw.
- `BatchExtra` / `BatchEvmSigner` — type contracts.

**We do NOT use the SDK's `GatewayClient.pay(url)` for the demo's hot path.**
Reason: `GatewayClient` signs against the `GatewayWallet` contract as the
EIP-712 `verifyingContract` (`extra.name === "GatewayWalletBatched"`), but
the demo's `agents/dark_pool.py` signs against the USDC contract directly
(`extra.name === "USDC"`). The two paths use the same EIP-3009 struct but
DIFFERENT EIP-712 domains — signatures from one are not valid for the other.

`X402BatchClient` therefore implements the x402 protocol directly. It picks
the EIP-712 domain per server response: if the server's `accepts[i].extra`
contains `name === "GatewayWalletBatched"`, we sign against the Gateway
domain; otherwise we sign against the USDC domain.

**As of Phase 2 / Bug 3 the dark pool now advertises BOTH `accepts[]`
entries**: `accepts[0]` is direct USDC (verifyingContract = USDC token)
and `accepts[1]` is `GatewayWalletBatched` (verifyingContract = Circle
GatewayWallet at `0x0077777d7EBA4688BDeF3E311b846F25870A19B9` on Arc
testnet). `X402BatchClient.pickAccept` filters by `network` + `asset`
and currently picks `accepts[0]` (direct USDC) — to drive the Gateway
path, either reorder the accepts entries on the server or extend
`pickAccept` to honour a `preferredDomainName` filter.

Server-side, `agents/dark_pool.py::_validate_payment` accepts payments
signed against either domain: it tries direct-USDC first, then
GatewayWallet, and returns 200 on the first that recovers a matching
signer. Tests covering both code paths live in
`agents/tests/test_dark_pool.py`
(`test_dark_pool_advertises_gateway_batched_scheme`,
`test_dark_pool_accepts_signed_payment_against_gateway_domain`).

The `CircleGatewaySettler` class wraps the SDK so that, when the dark pool
is later extended to advertise the Gateway batched option, broadcast-on-
flush can be enabled without touching client code. See
`circle_gateway.ts:settleViaSdk()`.

## Wire format — EXACTLY what `agents/dark_pool.py` expects

We mirrored these byte-for-byte from
`agents/dark_pool.py::build_signed_payment_header` and the Python client
in `agents/x402_client.py` so there's no integration drift:

### 402 response

```json
{
  "x402Version": 1,
  "accepts": [{
    "scheme": "exact",
    "network": "arc-testnet",
    "maxAmountRequired": "1000",
    "resource": "/query",
    "description": "RaBitQ dark pool query",
    "mimeType": "application/json",
    "payTo": "0x...",
    "maxTimeoutSeconds": 60,
    "asset": "0x3600000000000000000000000000000000000000",
    "extra": { "name": "USDC", "version": "2" }
  }]
}
```

### `X-PAYMENT` header (base64-encoded JSON)

```json
{
  "x402Version": 1,
  "scheme": "exact",
  "network": "arc-testnet",
  "payload": {
    "signature": "0x...65 bytes...",
    "authorization": {
      "from":         "0x...",
      "to":           "0x...",
      "value":        "1000",
      "validAfter":   "1716...",
      "validBefore":  "1716...",
      "nonce":        "0x...32 bytes..."
    }
  }
}
```

All numeric fields are **strings on the wire**; the server casts them
to `int` server-side. Getting the type wrong silently breaks EIP-712
hashing.

### EIP-712 domain

```typescript
{
  name: "USDC",          // "GatewayWalletBatched" in Gateway mode
  version: "2",          // "1" in Gateway mode
  chainId: 5042002,
  verifyingContract: "0x3600000000000000000000000000000000000000"
                         // GatewayWallet addr in Gateway mode
}
```

### EIP-712 type definition (must match field order)

```typescript
TransferWithAuthorization: [
  { name: "from", type: "address" },
  { name: "to", type: "address" },
  { name: "value", type: "uint256" },
  { name: "validAfter", type: "uint256" },
  { name: "validBefore", type: "uint256" },
  { name: "nonce", type: "bytes32" }
]
```

## Tests

13 vitest tests in `tests/batch_client.test.ts`:

| Test | What it proves |
|---|---|
| `test_sign_real_eip712_round_trip` | `viem.recoverTypedDataAddress(signed)` === EOA address. **No mocks.** Proves the domain matches Arc USDC. |
| USDC base unit conversion | No float drift on `0.001` / `1.000001` / `0.000001` boundaries |
| `pickAccept` amount guard | Throws `X402AmountExceededError` when server demands too much |
| Authorization digest is deterministic | Same inputs → same `sha256` over X-PAYMENT bytes |
| `test_pay_against_real_dark_pool_server` | **Real subprocess** — `agents/dark_pool.py` spawned via `uvicorn`, real HTTP `fetch`, real ecrecover on the server side |
| `test_batch_flushes_at_capacity` | `auto` mode auto-flushes when queue hits `maxBatchSize` |
| `test_batch_flushes_at_age` | `auto` mode auto-flushes when oldest item is older than `maxBatchAgeSeconds` (4s real wait) |
| `test_max_amount_guard` | Server prices at 0.005 USDC, client cap 0.001 → throws before signing, queue stays empty |
| `test_replay_protection_returns_402` | Same nonce reused → server returns 402 with `nonce replayed` error (proves Slice 5B's nonce store is wired) |
| `test_immediate_mode_does_not_queue` | `immediate` mode keeps queue size at 0 |
| `test_server_refusal_surfaces_400` | Wrong vector dim → server 400 → wrapped as `X402ServerRefusedError` |
| `close()` is idempotent | Second close returns zero-item settlement |
| `describe()` returns a status string | Smoke test for human-readable state |

Run:

```bash
node node_modules/vitest/vitest.mjs run scripts/x402/tests --reporter=verbose
```

Result on a clean run:

```
Test Files  1 passed (1)
     Tests  13 passed (13)
  Duration  ~9s
```

## Real-broadcast checklist (gated on user action)

For the demo's "we actually settled this on Arc" claim, the user must:

1. **Fund a Gateway depositor wallet** with 5–10 testnet USDC from
   <https://faucet.circle.com>. The wallet's private key becomes
   `GATEWAY_PRIVATE_KEY`.
2. **Deposit into Circle Gateway** (one-time):
   ```typescript
   import { CircleGatewaySettler } from "./circle_gateway.js";
   const settler = new CircleGatewaySettler({
     gatewayPrivateKey: process.env.GATEWAY_PRIVATE_KEY as Hex,
     chainName: "arcTestnet",
   });
   await settler.deposit("5");   // 5 USDC into Gateway Wallet
   ```
3. **Server already advertises both schemes** as of Phase 2 / Bug 3 —
   `agents/dark_pool.py::_payment_requirements` returns the direct-USDC
   entry as `accepts[0]` and the GatewayWalletBatched entry as
   `accepts[1]`. The server-side validator (`_validate_payment`) accepts
   payments signed against either EIP-712 domain.
4. **Switch `gatewayBatchMode: "auto"` with `gatewayPrivateKey` set on
   `X402BatchClient`** to enable real broadcast on flush.

For the demo we keep `broadcast: false` and just digest the batch. The
demo proof point is that **every authorization is server-verified to
match a real ecrecover** against the canonical Arc USDC domain — that's
the same hash a real broadcast would commit to.

## Turnkey signing path — explicitly deferred to Phase 4

`signer.ts::TurnkeyEoaSigner.signTypedData` currently requires a local
private key (`RawEoa.privateKey`). When called against a Turnkey-backed
EOA (key material in the TEE, `privateKey` undefined) it throws
`SignerUnusableError` with the message *"EOA has no privateKey
(Turnkey-backed). Use a Turnkey signer path from scripts/wallet/ for real
broadcast, or pass a local EOA (forceLocal=true) for dry-run signing."*

Why we punt instead of wiring `@turnkey/sdk-server.signTypedData`:

1. The only way to validate the integration is end-to-end against a
   real Turnkey org + sub-org + private-key resource — we do not have
   those credentials in this hackathon environment. A mocked test would
   only prove the mock works (per the repo's "real I/O" mandate).
2. The demo's hot path uses local EOAs spawned by
   `scripts/wallet/turnkey_client.ts::createTurnkeyEoa` with
   `forceLocal: true` — so the `SignerUnusableError` path is not
   exercised today.
3. The wire shape we'd hand to Turnkey (EIP-712 typed-data with the
   exact field order in `signer.ts::EIP3009_TRANSFER_TYPES` and the
   domain resolved by `resolveDomain`) is already what
   `recoverTypedDataAddress` validates against — i.e. the message format
   is locked. Plugging Turnkey in becomes a 20-LoC change to
   `TurnkeyEoaSigner.signTypedData`: dispatch to
   `turnkeyClient.apiClient().signRawPayload` with `payload = EIP-712
   digest`, then re-format the returned r/s/v into a `0x`-prefixed
   65-byte hex.

Tracking this as Phase 4 because the gating constraint is *credentials +
on-chain proof*, not *code volume*.

## Open questions / ambiguities in `use-gateway.md`

Flagged for the user to clarify with Circle if a real Gateway settlement
becomes a demo requirement:

1. `use-gateway.md` doesn't document the precise REST endpoint for
   submitting a *pre-signed* batch of authorizations as a single facilitator
   call. The SDK's flow assumes one URL per `pay()`. If a true "batch of N
   authorizations → 1 settlement" endpoint exists, it would let us flush
   ≥100 sigs in a single Gateway call (the spec §8 claim of "1 tx per
   ~1000 signatures"). I could not find it in the public docs and the
   SDK does not expose it.
2. The Gateway batched scheme requires the buyer to have deposited into
   `gatewayWallet`. We have not deposited; the demo uses direct USDC mode.
   If we deposit and the buyer subsequently uses the Gateway scheme, the
   USDC stays in `gatewayWallet` until the buyer calls `withdraw()`
   — confirm this is acceptable for the demo (vs simply spending from the
   buyer's EOA).
