# Agent Arena — Live Launcher Runbook

`scripts/arena_live.sh` is the **one command** that brings the AgoraHack
"Agent Arena" up on **real Arc testnet**: it deploys the contracts (including
`AgentRegistry`), registers 3 seed agents, and (optionally) starts the
continuous runner so the on-chain economy is populated and keeps updating.

No mocks. No demo mode. Every transaction is real and signed in-process (the
deployer key never touches argv or a child process env — see
`scripts/lib/chain.py`).

---

## TL;DR — the command you run

```bash
bash scripts/arena_live.sh --account arc-deployer --yes-i-understand --run
```

That single command:

1. Deploys all contracts via `forge script Deploy --broadcast` (incl.
   `AgentRegistry`), capturing the addresses from the broadcast log.
2. Runs `python -m scripts.arena_seed` to register **3 agents**.
3. Prints the exact `NEXT_PUBLIC_AGENT_REGISTRY=0x…` line for the UI.
4. With `--run`, starts `agent_runner --interval 15` in the **foreground**
   (continuous; Ctrl-C to stop). Drop `--run` to seed only.

It **refuses without `--yes-i-understand`** (real USDC is spent):

```bash
$ bash scripts/arena_live.sh
REFUSING to launch: arena_live.sh broadcasts REAL transactions to Arc testnet ...
No transaction was sent.
```

---

## Prerequisites

- **Foundry** (`forge`, `cast`, `anvil`) on `PATH`.
- **Contracts built**: `forge build` in `contracts/` (the seeder + tests read
  the compiled artifacts under `contracts/out/`).
- **`$RPC`** — Arc testnet RPC. Sourced automatically from `~/.arc-canteen/env`
  if present, or pass `--rpc-url`.
- **A funded operator key**, provided as an encrypted Foundry keystore
  (preferred):
  ```bash
  cast wallet import arc-deployer --interactive   # writes ~/.foundry/keystores/arc-deployer
  ```
  Then `--account arc-deployer`. The password comes from `$KEYSTORE_PASSWORD`
  or an interactive prompt — it is never logged. (Fallback: `export DEPLOYER_PK=0x…`.)
- **Fund the operator** with USDC on Arc testnet: <https://faucet.circle.com>.
  Recommended: **≥ 5 USDC** (gas is well under 1 USDC; 3 USDC are the
  recoverable bond stakes; the rest is headroom).

---

## Cost estimate

Arc testnet uses USDC as the native gas token (6 decimals). One full launch is
roughly **~23 transactions**:

| Phase                         | Count | Notes                                  |
|-------------------------------|-------|----------------------------------------|
| Contract deploys (Deploy.s)   | ~8    | incl. `AgentRegistry`                  |
| ERC-8004 identity mints       | 3     | one real identity per agent (0x8004…)  |
| Bond approves + posts         | 3 + 3 | 1 USDC bond each (recoverable stake)   |
| `defineConstitution`          | 3     | one distinct constitution per agent    |
| `AgentRegistry.register`      | 3     | one `AgentRegistered` event each       |

**Gas total: well under ~1 USDC.** Plus **3 USDC** locked as bonds (these are
your agents' stake — recoverable, not burned, unless an agent is slashed). So
budget ~4 USDC spent, ~3 of which is recoverable stake. Fund **≥ 5 USDC** for
comfort.

The launcher prints this estimate and waits 3 seconds before broadcasting
(Ctrl-C to abort).

---

## The seed flow (per agent)

For each of the N agents, in the exact order `AgentRegistry.register` requires
(`scripts/arena_seed.py::seed_arena`):

1. **Mint an ERC-8004 identity** owned by the operator — real
   `register(string,(string,bytes)[])` on the Arc identity registry
   `0x8004A818BFB912233c491871b3d84c89A494BD9e` (reuses
   `scripts.demo_e2e.register_identity`). Distinct identity per agent.
2. **Post a bond** (1 USDC default) in the shared `BondVault` — `approve` USDC
   then `post(amount)` (reuses `scripts.demo_e2e.post_bond`). Satisfies
   `AgentRegistry`'s `NoBond` guard.
3. **Define + hash a constitution** on chain via
   `ConstitutionRegistry.defineConstitution` (reuses
   `scripts.demo_e2e.define_constitution`). Each agent gets a genuinely
   different rule set, so the constitution hashes differ.
4. **`AgentRegistry.register(identityId, constitutionHash, bondVault,
   darkPoolUrl)`** — the `AgentRegistered` event yields the 1-indexed `agentId`.

### Operator model (disclosed, not faked)

For the demo all 3 agents share **one** signer — the deployer/operator key.
They are *operated by one key* but are *distinct on-chain entities*: each holds
its **own ERC-8004 identity NFT** (minted in step 1) and its **own
`AgentRegistry` agentId** (one agent per identity, enforced on-chain by
`IdentityAlreadyRegistered`). A single operator controlling several distinct
agents is exactly the multi-agent arena topology.

---

## Output

The seeder prints a JSON summary and writes `deployments/arena.json`:

```json
{
  "registry_addr": "0x…",
  "operator": "0x…",
  "chain_id": 5042002,
  "agents": [
    {"agent_id": 1, "identity_id": …, "register_tx": "0x…",
     "constitution_hash": "0x…", "dark_pool_url": "…", "explorer": "https://…"},
    …
  ]
}
```

---

## Wire the UI

Paste the printed line into `web/apps/web/.env.local`:

```
NEXT_PUBLIC_AGENT_REGISTRY=0x…   # the AgentRegistry address from step 3
```

The UI decodes the `AgentAction` event feed from that address to render the
living economy.

---

## Watch it live

```bash
# The seeded roster
cat deployments/arena.json

# Explorer (every register + recordAction shows up here)
open "https://testnet.arcscan.app/address/$(jq -r .registry_addr deployments/arena.json)"
```

With `--run`, the foreground runner logs one line per 15s cycle: each cycle
publishes fresh advice and emits a real `AgentAction` per agent — a new
on-chain event every cycle, which is what the UI's live feed streams.

To run the continuous loop separately (e.g. after seeding without `--run`):

```bash
agents/.venv/bin/python -m agents.agent_runner \
  --registry "$(jq -r .registry_addr deployments/arena.json)" \
  --agents 1,2,3 --rpc-url "$RPC" --account arc-deployer --interval 15
```

---

## Tests

The register→agentCount path is proven hermetically on an anvil fork (no Arc
broadcast, no key):

```bash
agents/.venv/bin/python -m pytest scripts/tests/test_arena_seed.py -q
```

asserts: 2 `AgentRegistered` events + `AgentRegistry.agentCount() == 2` +
`arena.json` written.
