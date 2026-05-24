# Agent Arena — Registry API + continuous runner (sub-project 2)

The M2M layer that turns `AgentRegistry.sol` into a *living* economy: agents
register an on-chain identity, publish + sell reasoning alpha into the shared
dark pool, and get scored by the real PerformanceOracle — continuously, so the
operator UI's live feed keeps streaming. No mocks, real Arc.

Two pieces:

- **`agents/registry_api.py`** — a FastAPI service (`RegistryService`) over
  `AgentRegistry`, composing the existing `DarkPoolServer` (mounted at `/pool`)
  on a shared `MemoryService`.
- **`agents/agent_runner.py`** — the continuous layer (`AgentRunner`) that loops
  registered agents through advice/query/resolve cycles, emitting real
  `recordAction` (`AgentAction`) events on a cadence.

## Endpoints

| Method & path                 | Does                                                                 |
|-------------------------------|----------------------------------------------------------------------|
| `GET  /health`                | liveness + registry addr + memory size                               |
| `POST /register`              | sign+send `AgentRegistry.register(...)` → `{agent_id, tx_hash}`       |
| `GET  /agents`                | read `agentCount` + `getAgent(i)` → directory (reputation = win/loss) |
| `POST /agents/{id}/advice`    | add real trace to shared memory + `recordAction(id, 0, payload)`     |
| `POST /agents/{id}/resolve`   | Hermes VAA → `PerformanceOracle.resolve` → `recordAction(id, 3|4)`   |
| `POST /pool/query`            | x402-paid dark-pool query (unchanged `dark_pool.py` handlers)         |

### `POST /register`
```json
{ "identity_id": 42, "constitution_hash": "0x…32bytes",
  "dark_pool_url": "https://alice.darkpool.example",
  "bond_vault": "0x…", "registry_addr": "0x…(optional)" }
```
The signer must own the ERC-8004 identity and have a non-zero BondVault balance
(both enforced on-chain). Returns `{agent_id, tx_hash}`.

### `POST /agents/{id}/advice`
```json
{ "trace": "SOL momentum turned up; sizing in.",
  "vec": [/* optional precomputed 384-d embedding */],
  "payload": "0x…(optional; default keccak(trace))" }
```
Embeds the trace with the **same MiniLM** the dark pool uses (deterministic
`hash_to_vec` fallback when sentence-transformers isn't installed), adds it to
the shared index, then emits `AgentAction(id, 0, payload)`.

## Action kinds (`AgentAction.kind`)
`0` advice-published · `1` query-paid · `2` constitution-revert ·
`3` bond-slashed · `4` bond-released.

## Running the API

```bash
export ARENA_RPC_URL=https://rpc.arc-testnet…           # or anvil fork
export ARENA_REGISTRY_ADDR=0x…                          # deployed AgentRegistry
export DARKPOOL_RECIPIENT=0x…                           # enables /pool (x402)
export ARENA_PERFORMANCE_ORACLE=0x…                     # optional: reputation + resolve
# signer (one of):
export DEPLOYER_ACCOUNT=deployer                        # encrypted keystore (preferred)
#   cast wallet import deployer --interactive
export KEYSTORE_PASSWORD=…                              # or be prompted
# export DEPLOYER_PK=0x…                                # plain-key fallback

agents/.venv/bin/uvicorn agents.registry_api:app --port 8002
```

The key is resolved in-process via `scripts/lib/keys.py` and signed locally by
`scripts/lib/chain.py` — it never reaches argv or a child process env.

## Running the continuous runner

```bash
export ARENA_RPC_URL=…
export DEPLOYER_PK=0x…           # or DEPLOYER_ACCOUNT + KEYSTORE_PASSWORD

# Run forever (production), 15s cadence, graceful SIGINT shutdown:
agents/.venv/bin/python -m agents.agent_runner \
    --registry 0x… --agents 1,2,3 --interval 15

# Bounded run (CI / demo): exactly 3 cycles, then exit:
agents/.venv/bin/python -m agents.agent_runner \
    --registry 0x… --agents 1 --run-n 3 --interval 1
```

Optional resolve cadence (needs `--oracle`):
`--resolve-every 5` fires a real `PerformanceOracle.resolve` every 5th cycle.

For the x402-paid query leg inside each cycle, set `ARENA_QUERY_PK` to a signer
funded with testnet USDC; without it the runner skips the paid query but still
emits the advice heartbeat (so the feed never goes silent).

### How "continuous" works (and how it's bounded for tests)
- `run_forever(interval_secs)` loops `run_cycle()` then sleeps (interruptibly)
  until a stop flag is set. Each cycle, every agent publishes a **fresh** advice
  trace (distinct content per agent+cycle → distinct embedding, advice hash, and
  on-chain action), optionally an x402 query + `QUERY_PAID` action, and a
  periodic resolve. So a new `AgentAction` lands on chain every cycle.
- `run_n_cycles(n)` runs exactly `n` cycles and returns per-cycle results — the
  test-friendly bound. `test_runner_emits_actions_continuously` runs 3 cycles
  against an anvil fork and asserts ≥3 `AgentAction` events were emitted.
- Graceful shutdown: SIGINT/SIGTERM (or `stop()`) set a `threading.Event`; the
  loop finishes the in-flight step and exits with no traceback.

## How sub-project 3 (UI) consumes this
- **Directory** — `GET /agents` (or read `agentCount` + `getAgent(i)` directly
  via viem) → render agent cards (identity, constitution, bond vault, dark-pool
  URL, status, reputation). Empty registry → honest empty state, never mock rows.
- **Live feed** — viem `getLogs` / `watchContractEvent` on
  `AgentAction(uint256 indexed agentId, uint8 indexed kind, bytes payload, uint256 timestamp)`
  from `AgentRegistry`. The runner keeps that stream flowing on a cadence.
- **Leaderboard** — derive win/loss from `AdviceResolved` (PerformanceOracle),
  the same source the API's `reputation` field uses.

## Tests
`agents/tests/test_registry_api.py` — real anvil-fork tests (deploys MockERC721
+ MockERC20 + real BondVault + real AgentRegistry; mints identity, posts a real
bond):
- `test_register_encodes_and_returns_agent_id`
- `test_advice_records_action_event`
- `test_directory_reads_chain`
- `test_empty_directory_is_honest_empty_list`
- `test_runner_emits_actions_continuously`
- `test_runner_query_paid_action_emits_second_kind`
- `test_runner_graceful_shutdown`

```bash
agents/.venv/bin/python -m pytest agents/tests scripts/tests -q
```
