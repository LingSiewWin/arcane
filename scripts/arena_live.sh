#!/usr/bin/env bash
# arena_live.sh — the ONE command that brings the Agent Arena up on REAL Arc
# testnet: deploy the contracts (incl. AgentRegistry), seed 3 agents, and
# (optionally) start the continuous runner so the economy is populated and
# keeps updating.
#
# This is the live, real-money path — it broadcasts real transactions and
# burns real faucet USDC. It REFUSES to do anything without --yes-i-understand.
#
# Usage:
#   bash scripts/arena_live.sh                                  # refuses (safe)
#   bash scripts/arena_live.sh --account arc-deployer --yes-i-understand
#   bash scripts/arena_live.sh --account arc-deployer --yes-i-understand --run
#
# Flags:
#   --account <name>     Foundry keystore name (preferred; key never in argv).
#   --yes-i-understand   REQUIRED. Confirms you accept real USDC spend.
#   --run                After seeding, start agent_runner --interval 15 in the
#                        FOREGROUND (continuous; Ctrl-C to stop).
#   --n <int>            Number of seed agents (default 3).
#   --rpc-url <url>      Override $RPC.
#
# Env (sourced from ~/.arc-canteen/env if present):
#   RPC                  Arc testnet RPC URL (required).
#   KEYSTORE_PASSWORD    optional; else you're prompted for the keystore pass.
#
# Cost estimate (Arc testnet, USDC = native gas, 6 decimals):
#   ~8 contract deploys           +  ~3 ERC-8004 identity mints
#   ~3 bond approves + 3 posts    +  ~3 defineConstitution + 3 register
#   => ~23 txs. At Arc testnet gas this is well under ~1 USDC total; fund the
#   operator with >= 5 USDC (https://faucet.circle.com) for comfortable
#   headroom (bonds themselves are 1 USDC each = 3 USDC of recoverable stake).

set -euo pipefail

ACCOUNT=""
YES=0
RUN=0
N=3
RPC_FLAG=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --account)            ACCOUNT="$2"; shift 2 ;;
    --account=*)          ACCOUNT="${1#*=}"; shift ;;
    --yes-i-understand)   YES=1; shift ;;
    --run)                RUN=1; shift ;;
    --n)                  N="$2"; shift 2 ;;
    --n=*)                N="${1#*=}"; shift ;;
    --rpc-url)            RPC_FLAG="$2"; shift 2 ;;
    --rpc-url=*)          RPC_FLAG="${1#*=}"; shift ;;
    -h|--help)            sed -n '1,40p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "arena_live.sh: unknown arg: $1" >&2; exit 2 ;;
  esac
done

# --- Safety gate FIRST: refuse before any env/RPC/broadcast work -------------
if [[ "$YES" -ne 1 ]]; then
  cat >&2 <<'REFUSE'
REFUSING to launch: arena_live.sh broadcasts REAL transactions to Arc testnet
and spends REAL faucet USDC (contract deploys + 3 identity mints + 3 bonds +
3 constitutions + 3 registers).

Re-run with --yes-i-understand once you have funded the operator. Example:

  bash scripts/arena_live.sh --account arc-deployer --yes-i-understand --run

No transaction was sent.
REFUSE
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
CONTRACTS_DIR="$REPO_ROOT/contracts"
DEPLOYMENTS_DIR="$REPO_ROOT/deployments"
PY="$REPO_ROOT/agents/.venv/bin/python"
[[ -x "$PY" ]] || PY="python3"

# Load $RPC (and any KEYSTORE_PASSWORD) from the canteen env if present.
if [[ -f "$HOME/.arc-canteen/env" ]]; then
  # shellcheck disable=SC1091
  . "$HOME/.arc-canteen/env"
fi
RPC_EFFECTIVE="${RPC_FLAG:-${RPC:-}}"

if [[ -z "$RPC_EFFECTIVE" ]]; then
  echo "arena_live.sh: need RPC. Set \$RPC (~/.arc-canteen/env) or pass --rpc-url." >&2
  exit 3
fi
if [[ -z "$ACCOUNT" && -z "${DEPLOYER_PK:-}" && -z "${DEPLOYER_ACCOUNT:-}" ]]; then
  echo "arena_live.sh: need a signer. Pass --account <keystore> (preferred) or set DEPLOYER_PK." >&2
  exit 3
fi
for bin in forge cast; do
  command -v "$bin" >/dev/null 2>&1 || { echo "arena_live.sh: '$bin' not on PATH (install Foundry)." >&2; exit 4; }
done

mask_rpc() { echo "$1" | sed -E 's#(swrm_)[A-Za-z0-9]+#\1<redacted>#g'; }

echo "================ Agent Arena — LIVE on Arc testnet ================"
echo "  account : ${ACCOUNT:-<DEPLOYER_PK/DEPLOYER_ACCOUNT>}"
echo "  rpc     : $(mask_rpc "$RPC_EFFECTIVE")"
echo "  agents  : $N"
echo "  est cost: ~23 txs (deploys + 3 identity + 3 bond + 3 const + 3 reg)"
echo "            < ~1 USDC gas + ${N} USDC recoverable bond stake."
echo "            fund operator >= 5 USDC: https://faucet.circle.com"
echo "  run     : $([[ "$RUN" -eq 1 ]] && echo 'yes (continuous runner)' || echo 'no')"
echo "==================================================================="
echo "Broadcasting in 3 seconds. Ctrl-C now to abort."
sleep 3

mkdir -p "$DEPLOYMENTS_DIR"
RUN_LOG="$DEPLOYMENTS_DIR/arena.deploy.log"

# --- (1) Deploy all contracts (incl. AgentRegistry) via forge script ---------
# Resolve the deployer key in-process (eth_account) and pass it ONLY as the
# forge env var PRIVATE_KEY (never on argv via --private-key). For keystore
# accounts we decrypt via scripts.lib.keys; the raw key lives only in this
# command substitution's env, never in `ps`.
echo
echo "[1/3] deploying contracts (forge script Deploy --broadcast) ..."

resolve_key() {
  ACCOUNT="$ACCOUNT" "$PY" - <<'PYEOF'
import os, sys
sys.path.insert(0, os.environ.get("REPO_ROOT", "."))
from scripts.lib.keys import resolve_deployer_key, KeyResolutionError
acct = os.environ.get("ACCOUNT") or None
try:
    print(resolve_deployer_key(account=acct, allow_interactive=True))
except KeyResolutionError as e:
    sys.stderr.write(f"REFUSING: {e}\n")
    sys.exit(3)
PYEOF
}

# Decrypt once, keep in a shell variable in THIS process only.
export REPO_ROOT
DEPLOYER_PK_RESOLVED="$(resolve_key)" || { echo "arena_live.sh: could not resolve deployer key." >&2; exit 3; }

# Run forge from INSIDE the contracts dir: forge resolves the script path
# relative to cwd (not --root), so a contracts-relative path fails from the
# repo root with "No such file or directory". RUN_LOG is absolute, so tee is
# safe across the cd. No --silent: we need the console2.log address lines that
# extract() parses below.
(
  cd "$CONTRACTS_DIR" && \
  PRIVATE_KEY="$DEPLOYER_PK_RESOLVED" forge script \
    "script/Deploy.s.sol:Deploy" \
    --rpc-url "$RPC_EFFECTIVE" \
    --broadcast \
    --slow
) 2>&1 | tee "$RUN_LOG"

# Parse addresses from the console2.log lines (forge prints "Name: 0x...").
extract() {
  grep -E "^[[:space:]]*$1[[:space:]]*:[[:space:]]+0x[0-9a-fA-F]{40}" "$RUN_LOG" \
    | tail -1 | awk '{print $NF}'
}
REG_ADDR="$(extract 'ConstitutionRegistry' || true)"
BOND_ADDR="$(extract 'BondVault' || true)"
AGENT_REGISTRY_ADDR="$(extract 'AgentRegistry' || true)"

if [[ -z "$REG_ADDR" || -z "$BOND_ADDR" || -z "$AGENT_REGISTRY_ADDR" ]]; then
  echo "arena_live.sh: could not parse required addresses from forge output." >&2
  echo "  ConstitutionRegistry=$REG_ADDR BondVault=$BOND_ADDR AgentRegistry=$AGENT_REGISTRY_ADDR" >&2
  echo "  see $RUN_LOG" >&2
  exit 5
fi

echo
echo "  ConstitutionRegistry: $REG_ADDR"
echo "  BondVault           : $BOND_ADDR"
echo "  AgentRegistry       : $AGENT_REGISTRY_ADDR"

# --- (2) Seed N agents -------------------------------------------------------
echo
echo "[2/3] seeding $N agents (mint identity -> post bond -> define -> register) ..."

SEED_ARGS=(--rpc-url "$RPC_EFFECTIVE"
  --registry "$AGENT_REGISTRY_ADDR"
  --bond-vault "$BOND_ADDR"
  --constitution-registry "$REG_ADDR"
  --n "$N")
if [[ -n "$ACCOUNT" ]]; then
  SEED_ARGS+=(--account "$ACCOUNT")
fi

# The seeder resolves the key in-process the same way (keystore/account or
# DEPLOYER_PK env). We do NOT pass the raw key on argv.
( cd "$REPO_ROOT" && "$PY" -m scripts.arena_seed "${SEED_ARGS[@]}" )

# --- (3) UI env var ----------------------------------------------------------
echo
echo "[3/3] UI wiring — paste this into web/apps/web/.env.local :"
echo
echo "    NEXT_PUBLIC_AGENT_REGISTRY=$AGENT_REGISTRY_ADDR"
echo
echo "Arena is live + populated. Watch it:"
echo "    cat $DEPLOYMENTS_DIR/arena.json"
echo "    explorer: https://testnet.arcscan.app/address/$AGENT_REGISTRY_ADDR"

# --- Optional: continuous runner (foreground) --------------------------------
if [[ "$RUN" -eq 1 ]]; then
  # Build a comma-separated agent-id list 1..N (agentIds are 1-indexed).
  IDS="$(seq -s, 1 "$N")"
  echo
  echo "Starting continuous runner (interval 15s, agents $IDS). Ctrl-C to stop."
  echo
  RUNNER_ARGS=(--registry "$AGENT_REGISTRY_ADDR" --agents "$IDS"
    --rpc-url "$RPC_EFFECTIVE" --interval 15)
  if [[ -n "$ACCOUNT" ]]; then
    RUNNER_ARGS+=(--account "$ACCOUNT")
  fi
  cd "$REPO_ROOT"
  exec env ARENA_RPC_URL="$RPC_EFFECTIVE" \
    "$PY" -m agents.agent_runner "${RUNNER_ARGS[@]}"
fi
