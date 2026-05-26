# Arcane Primitives — Reusable Building Blocks for Arc

Arcane is a live arena where sovereign **ERC-8004 AI agents** duel on-chain, settled in
Arc testnet USDC. Building it produced a set of standalone primitives that any Arc builder
can fork or import independently — agent identity, accountability, on-chain governance,
verifiable memory, and an adversarial benchmark.

This doc answers two questions:
1. **What reusable primitives does Arcane expose?**
2. **What does Arcane add on top of `circlefin/arc-*`?**

All paths below are real files in this repo. Snippets are taken from the actual code.

---

## Why this is missing from `circlefin/arc-*`

The reference `circlefin/arc-*` repos (e.g. `arc-commerce`, `arc-p2p-payments`) cover the
**payments base layer**: moving USDC, CCTP bridging, x402 commerce, merchant/settlement
flows. They answer "how do funds move on Arc."

They do **not** cover **autonomous agents as first-class on-chain actors**. Arcane adds the
agent layer on top of the same USDC settlement primitive:

| Concern | `arc-*` | Arcane adds |
|---|---|---|
| Move USDC / CCTP / commerce | ✅ | (reuses it — USDC is gas + settlement) |
| Agent self-sovereign onboarding (self-mint identity + self-stake) | — | ✅ `agents/provision.py` |
| Identity + bond + constitution binding, with slashing | — | ✅ `AgentRegistry` / `BondVault` / `PerformanceOracle` |
| On-chain constitution enforced at execution (ERC-7579) | — | ✅ Constitution stack |
| Verifiable, compressed agent memory anchored to identity | — | ✅ RaBitQ + `MemoryAnchor` |
| Attributed adversarial-resilience benchmark | — | ✅ `Colosseum` chaos ledger |
| Agent-to-agent **paid query** endpoint (x402 as a machine API, not checkout) | partial | ✅ `agents/dark_pool.py` + `x402_client.py` |

On Arc, USDC (`0x3600…0000`, 6 decimals) is **both the gas token and the settlement
asset**, so a single funded operator wallet fans out gas + stakes to a whole agent roster —
the foundation the onboarding flow below relies on.

---

## How to run locally

The canonical launcher is `scripts/arena_live.sh` (see `README.md` Quickstart):

```bash
# read-only web front door (points at live Arc public RPC by default)
cd web && bun install && bun run dev:web      # http://localhost:3001

# launch a live arena (spends real testnet USDC; refuses without the flag)
bash scripts/arena_live.sh --account arc-deployer --yes-i-understand

# keep an arena populated continuously
scripts/arena_forever.sh
```

The host orchestrator is `scripts/run_arena.py`: spawn keypairs → provision → run duels →
print Alpha + Iron Shield rankings. Every chain/price/embed seam is injectable, so the
wiring is unit-tested offline with no network and no provider key.

---

## 1. Self-sovereign agent onboarding

**What it is.** A fresh EVM keypair becomes a real on-chain agent: the operator funds it a
little USDC, then the **agent itself** (its own key) self-mints its ERC-8004 identity and
self-stakes its bond — so the agent, not the operator, owns its identity and has skin in
the game.

**Where it lives.** `agents/provision.py` (orchestration) + `agents/agent_wallet.py`
(`spawn_keypairs`, encrypted keystores) + `scripts/lib/chain.py` (in-process signing).

**How to use / fork it.** `provision_agent` runs four ordered steps (USDC is gas + stake,
so funding must come first):

```python
from agents.agent_wallet import spawn_keypairs
from agents.provision import provision_agents

wallets = spawn_keypairs(3, password="…")           # 3 encrypted keystores, keys in-memory
provision_agents(
    wallets,
    rpc_url="https://rpc.testnet.arc.network",
    operator_pk=OPERATOR_PK,                          # funds each agent (signed by operator)
    colosseum=COLOSSEUM_ADDR,
    fund_usdc=1.0, stake_usdc=1.0,                    # fund must cover stake + gas
)
# Per agent, in strict order (steps 2-4 signed by the AGENT's own key):
#   1. operator  transfer(agent, fund_units)         on USDC
#   2. agent     self-mints ERC-8004 identity        -> wallet.identity_id set
#   3. agent     approve(colosseum, stake_units)      on USDC
#   4. agent     registerAgent(agent)                 on Colosseum  (msg.sender == developer)
```

The two chain side-effects (`send_fn`, `register_identity_fn`) are injectable, so the
sequence is unit-testable with zero network. Keys live only in `AgentWallet` objects, are
scrypt-encrypted at rest in the gitignored `agents/.arena_keystore/`, and are never logged
or placed on argv.

---

## 2. Accountability contracts

**What it is.** The trustless backbone that binds an agent's identity to a constitution, a
slashable bond, and an endpoint — and ranks agents by performance and resilience.

**Where it lives.** `contracts/src/AgentRegistry.sol`, `BondVault.sol`,
`PerformanceOracle.sol`, `Colosseum.sol`.

**How to use / fork it.**

`AgentRegistry.register` enforces identity ownership **and** a non-zero bond (one agent per
identity):

```solidity
function register(
    uint256 identityId,        // ERC-8004 NFT the caller must own (ownerOf check)
    bytes32 constitutionHash,  // rules the agent commits to
    address bondVault,         // must hold a non-zero bond for msg.sender
    string  calldata darkPoolUrl
) external returns (uint256 agentId);
// reverts: NotIdentityOwner | NoBond | IdentityAlreadyRegistered
```
It also emits an `AgentAction(agentId, kind, payload, ts)` live feed (kinds 0–4:
ADVICE_PUBLISHED, QUERY_PAID, CONSTITUTION_REVERT, BOND_SLASHED, BOND_RELEASED).

`BondVault` is a slashable stake escrow with three notable mechanics: Numerai **Erasure
double-burn** (`slash` burns both the agent's bond and the slasher's counter-bond to
`0x…dEaD`, so slashing can't be profited from), an **Olas-style liveness signal**
(`pokeActivity` / `releaseToOperator` rescues a dead agent's bond to its funder), and an
OpenZeppelin `Pausable` circuit breaker (`release()` stays open while paused).

`PerformanceOracle` turns trading advice into a market-driven verdict from Pyth prices:
`r_bps = direction * (p1 - p0) * 10000 / |p0|`. `resolve()` is permissionless after the
horizon; it slashes only when the adverse move clears the Pyth confidence band, and the
oracle must post its own counter-bond first.

`Colosseum` runs the duels: `registerAgent` (anti-spam stake), `createDuel`, parimutuel
`bet`/`claim` (winning side splits the whole pot pro-rata, dust-safe), `injectChaos`
(escrowed paid attacks), `reportCall` (recorder-reported scores + resilience), and
`resolve` paying **dual prize pools** — Alpha (PnL) winner and Iron Shield (resilience)
winner each take 50%. Money flows are fully on-chain; only scoring trusts the `recorder`.

---

## 3. On-chain Constitution stack (ERC-7579)

**What it is.** A versioned, content-addressed rule set an agent commits to, then enforced
at execution time — at validation (intent), as a hook (outcome), and through an executor
(executor-initiated calls). Closes the "validator-only" bypass where executor modules act
without triggering rule checks.

**Where it lives.** `contracts/src/ConstitutionRegistry.sol` + `ConstitutionValidator.sol`
(MODULE_TYPE_VALIDATOR) + `ConstitutionHook.sol` (MODULE_TYPE_HOOK, `preCheck`/`postCheck`)
+ `ConstitutionExecutor.sol` (MODULE_TYPE_EXECUTOR). Rule decoders live in
`contracts/src/adapters/`.

**How to use / fork it.** Publish a rule list to the registry (returns the canonical hash),
then install that hash on an ERC-7579 smart account:

```solidity
// 1. Define rules (content-addressed; same list -> same hash, free duplicates).
ConstitutionRegistry.Rule[] memory rules = new ConstitutionRegistry.Rule[](1);
rules[0] = ConstitutionRegistry.Rule({
    kind: 1,                              // MAX_TRADE_SIZE
    params: abi.encode(uint256(1e6)),     // 1 USDC cap
    adapter: address(0)                   // inline fast path; adapters extend to DEX/perp
});
bytes32 hash = registry.defineConstitution(rules);

// 2. Install on the SCA (onInstall(bytes32 hash) or (hash, entryPoint)).
//    validateUserOp / preCheck then revert("ConstitutionViolation:MAX_TRADE_SIZE") on breach.
```

Rule kinds: `0` MAX_LEVERAGE, `1` MAX_TRADE_SIZE, `2` VENUE_BLACKLIST,
`3` NO_UNAUDITED_CONTRACTS, `4` SUBDELEGATION_BOUND, `255` CUSTOM. Adapter-requiring kinds
(leverage, sub-delegation) are rejected at `defineConstitution` if their adapter is zero —
fail-closed at registration, not silently at runtime.

---

## 4. Verifiable agent memory (RaBitQ + MemoryAnchor)

**What it is.** A 1-bit RaBitQ semantic memory (~27× smaller than FP32) whose pinned-rule
Merkle root is anchored on-chain, keyed to the agent's ERC-8004 identity — tamper-evident
proof of what the agent actually compressed.

**Where it lives.** `agents/memory_service.py` (RaBitQ store + decay + Merkle root),
`agents/embedder.py` (MiniLM-L6-v2, 384-d), `contracts/src/MemoryAnchor.sol`.

**How to use / fork it.** Each entry stores only the packed sign bits (48 B at d=384) plus
two float scalars — **56 B/vec vs 1536 B for FP32 = 27.4×** (verified by
`MemoryService.memory_stats()`, not estimated):

```python
from agents.embedder import Embedder
from agents.memory_service import MemoryService

emb = Embedder()                                   # all-MiniLM-L6-v2, 384-d
mem = MemoryService(dim=384)
mem.add("rule-1", emb.embed("never exceed 1 USDC"), pinned=True)
root = mem.pinned_merkle_root()                    # deterministic bytes32
```

Then anchor the root to your ERC-8004 identity on-chain (caller must own the NFT):

```solidity
function anchor(uint256 identityId, bytes32 root) external;  // ownerOf(identityId) == msg.sender
// append-only history: anchorAt / historicalOwnerOf attribute each root even after NFT transfer
```

Off-chain observers recompute the root over the pinned slot each cycle; equality with the
on-chain anchor proves the agent still carries the rules it deployed with.

---

## 5. Adversarial-resilience harness (on-chain agent benchmark)

**What it is.** Attributed, on-chain **chaos injection** turns a duel into a reusable
benchmark: spectators pay USDC to hit a live agent with a pre-authored attack, and the
chain records who attacked whom and whether the agent survived. The chaos is the dataset.

**Where it lives.** `contracts/src/Colosseum.sol` (`injectChaos`, `reportCall`,
resilience accounting), driven by `agents/duelist.py` / `scripts/run_arena.py`.

**How to use / fork it.** Three pre-authored items (free-text injection is impossible
on-chain, so the dataset stays clean): `ITEM_FLASHBANG` (0), `ITEM_MEMORY_WIPE` (1),
`ITEM_LIQUIDITY_SHIELD` (2).

```solidity
uint256 injId = colosseum.injectChaos(duelId, targetAgent, ITEM_FLASHBANG);  // escrows fee
// recorder later settles it counterfactually:
colosseum.reportCall(duelId, agent, injId, rBps, /*ingested*/true, /*survived*/true, /*failed*/false);
```
Per agent across all duels: `resilience = survivedInjections / injectionsIngested`
(read via `resilienceOf(agent)`). A survived injection pays its escrow as a defense bounty
to the agent's developer; a fooled one fattens the duel prize pool — defense becomes revenue.

---

## 6. Patterns & infra

- **x402 agent-to-agent paid query** — `agents/dark_pool.py` wraps a `MemoryService`
  behind an HTTP-402 paywall (`POST /query` returns top-k for a 384-d vector). The client,
  `agents/x402_client.py`, runs the full handshake: parse `accepts`, sign an EIP-3009
  `TransferWithAuthorization`, retry with the `X-PAYMENT` header. It pins the recipient
  (`expected_recipient`) and enforces a strict price cap (`expected_price_usdc`) so a
  server can't swap the payee or quote above its advertised price. This is x402 as a
  **machine-to-machine API**, not human checkout.

- **Keyless read-only Arc dashboard** — `web/apps/web/src/lib/chain.ts` builds a viem
  `publicClient` against the public Arc RPC (`https://rpc.testnet.arc.network`) — no
  backend, no keys, no mocks. Reads contract state + watches events directly
  (`web/apps/web/src/lib/arena.ts`, `hooks.ts`). Unset addresses render honest empty states.

- **Deterministic ERC-8004 → pixel avatar** — `web/apps/web/src/lib/pixel-avatar.ts`.
  `avatarPixels(id)` hashes any stable string (an address/identity) via FNV-1a → mulberry32
  PRNG → fills a symmetric 16×16 humanoid mask. Same id ⇒ identical, mirror-symmetric pixel
  set, framework-free (no React/canvas). A free identicon for any agent identity.

- **In-process signer** — `scripts/lib/chain.py`. `cast_send` / `deploy_contract_via_cast`
  sign EIP-1559 txs with `eth_account` and broadcast via `eth_sendRawTransaction`. The
  private key never reaches argv or a child-process env (the `cast send --private-key`
  key-leak vector is eliminated). `cast call` (read-only) still subprocesses and refuses to
  run if a key is passed. Drop-in for any Arc script that must sign without leaking keys.
