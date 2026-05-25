#!/usr/bin/env bash
# arena_live.sh — bring The Arena up on REAL Arc testnet in one command:
# (re)use or deploy the stack (Colosseum + MemoryAnchor), then hand off to the
# Python launcher (scripts.run_arena), which spawns N autonomous agents,
# provisions each on Arc (operator funds -> agent self-mints its ERC-8004
# identity -> agent self-registers + stakes in the Colosseum), builds
# memory-augmented LLM duelists, runs a round of duels scored on REAL Pyth
# prices, and prints the Alpha + Iron Shield leaderboards plus the on-chain
# duel ids.
#
# Needs a model provider key — $OPENROUTER_API_KEY (OpenAI-compatible, routes to
# Claude/any model) or $ANTHROPIC_API_KEY. The duelists make real model calls —
# never faked. Broadcasts real transactions + spends faucet USDC for gas + each
# agent's stake. It REFUSES without --yes-i-understand.
#
# Usage:
#   bash scripts/arena_live.sh                                  # refuses (safe)
#   bash scripts/arena_live.sh --account arc-deployer --yes-i-understand
#   bash scripts/arena_live.sh --colosseum 0x... --memory-anchor 0x... \
#        --agents 4 --cycles 4 --symbol SOL --yes-i-understand
#
# Flags:
#   --account <name>       Foundry keystore (preferred; key never on argv). Or set DEPLOYER_PK.
#   --colosseum <addr>     Reuse an already-deployed Colosseum (skip deploy).
#   --memory-anchor <addr> Reuse an already-deployed MemoryAnchor (skip deploy).
#   --agents <n>           Number of autonomous agents to spawn (default 4).
#   --cycles <n>           Scored cycles per duel (default 4).
#   --symbol <SYM>         Pyth-scored asset (default SOL).
#   --rpc-url <url>        Override $RPC.
#   --yes-i-understand,-y  REQUIRED — confirms real USDC gas + stake spend.
#   --help,-h              Show this help.
set -euo pipefail

ACCOUNT=""; YES=0; COLOSSEUM=""; MEMORY_ANCHOR=""; AGENTS=4; CYCLES=4; SYMBOL="SOL"; RPC_FLAG=""; DURATION=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --account) ACCOUNT="$2"; shift 2 ;;
    --account=*) ACCOUNT="${1#*=}"; shift ;;
    --colosseum) COLOSSEUM="$2"; shift 2 ;;
    --colosseum=*) COLOSSEUM="${1#*=}"; shift ;;
    --memory-anchor) MEMORY_ANCHOR="$2"; shift 2 ;;
    --memory-anchor=*) MEMORY_ANCHOR="${1#*=}"; shift ;;
    --agents) AGENTS="$2"; shift 2 ;;
    --agents=*) AGENTS="${1#*=}"; shift ;;
    --cycles) CYCLES="$2"; shift 2 ;;
    --cycles=*) CYCLES="${1#*=}"; shift ;;
    --duration) DURATION="$2"; shift 2 ;;
    --duration=*) DURATION="${1#*=}"; shift ;;
    --symbol) SYMBOL="$2"; shift 2 ;;
    --symbol=*) SYMBOL="${1#*=}"; shift ;;
    --rpc-url) RPC_FLAG="$2"; shift 2 ;;
    --rpc-url=*) RPC_FLAG="${1#*=}"; shift ;;
    --yes-i-understand|-y) YES=1; shift ;;
    -h|--help) sed -n '1,34p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "arena_live.sh: unknown arg: $1" >&2; exit 2 ;;
  esac
done

# --- Safety gate FIRST: refuse before any env/RPC/broadcast work -------------
if [[ "$YES" -ne 1 ]]; then
  cat >&2 <<'REFUSE'
REFUSING: arena_live.sh broadcasts REAL transactions to Arc testnet and spends
faucet USDC for gas + each agent's stake (stack deploy + per-agent fund/identity/
register + createDuel + scored cycles + resolve). The duelists also make REAL
model calls. Re-run with --yes-i-understand once the operator is funded. Example:

  bash scripts/arena_live.sh --account arc-deployer --yes-i-understand

No transaction was sent.
REFUSE
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
CONTRACTS_DIR="$REPO_ROOT/contracts"
PY="$REPO_ROOT/agents/.venv/bin/python"; [[ -x "$PY" ]] || PY="python3"
export REPO_ROOT

# Load root .env (gitignored) for backend keys/RPC — no `export` needed. Then the
# canteen env (for RPC) if present. Already-exported shell vars still win because
# we only fill ones that are currently empty.
if [[ -f "$REPO_ROOT/.env" ]]; then
  while IFS='=' read -r k v; do
    k="${k// /}"  # tolerate "KEY = val" spacing
    # Only accept valid shell identifiers — never let a malformed .env line set
    # something like IFS/PATH.
    [[ "$k" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue
    v="${v%\"}"; v="${v#\"}"; v="${v%\'}"; v="${v#\'}"
    [[ -z "${!k:-}" ]] && export "$k=$v"
  done < "$REPO_ROOT/.env"
fi
# shellcheck disable=SC1091
[[ -f "$HOME/.arc-canteen/env" ]] && . "$HOME/.arc-canteen/env"
# Account can come from .env (DEPLOYER_ACCOUNT); an explicit --account flag wins.
ACCOUNT="${ACCOUNT:-${DEPLOYER_ACCOUNT:-}}"
RPC_EFFECTIVE="${RPC_FLAG:-${RPC:-}}"
[[ -n "$RPC_EFFECTIVE" ]] || { echo "need \$RPC or --rpc-url" >&2; exit 3; }
if [[ -z "$ACCOUNT" && -z "${DEPLOYER_PK:-}" ]]; then
  echo "need a signer: --account <keystore> or \$DEPLOYER_PK" >&2; exit 3
fi
command -v forge >/dev/null 2>&1 || { echo "forge not on PATH (install Foundry)" >&2; exit 4; }
if [[ -z "${OPENROUTER_API_KEY:-}" && -z "${ANTHROPIC_API_KEY:-}" ]]; then
  echo "need \$OPENROUTER_API_KEY (routes to Claude/any model) or \$ANTHROPIC_API_KEY" >&2
  echo "— the duelists make REAL model calls (never faked)." >&2
  exit 3
fi

mask() { echo "$1" | sed -E 's#(swrm_)[A-Za-z0-9]+#\1<redacted>#g'; }

# Resolve the deployer key in-process (never on argv). Reused for forge env +
# the launcher's operator DEPLOYER_PK.
resolve_key() {
  ACCOUNT="$ACCOUNT" "$PY" - <<'PYEOF'
import os, sys
sys.path.insert(0, os.environ.get("REPO_ROOT", "."))
from scripts.lib.keys import resolve_deployer_key, KeyResolutionError
try:
    print(resolve_deployer_key(account=os.environ.get("ACCOUNT") or None, allow_interactive=True))
except KeyResolutionError as e:
    sys.stderr.write(f"REFUSING: {e}\n"); sys.exit(3)
PYEOF
}
# Prefer a raw DEPLOYER_PK (no password) when set; else decrypt the keystore.
if [[ -n "${DEPLOYER_PK:-}" ]]; then
  PK_RESOLVED="$DEPLOYER_PK"
  echo "  signer  : DEPLOYER_PK (env)"
else
  PK_RESOLVED="$(resolve_key)" || { echo "could not resolve deployer key" >&2; exit 3; }
fi
# Derive the address WITHOUT putting the key on argv (it would show in `ps`).
DEPLOYER_ADDR="$(PK="$PK_RESOLVED" "$PY" -c "import os; from eth_account import Account; print(Account.from_key(os.environ['PK']).address)")"

echo "================ The Arena — LIVE on Arc ================"
echo "  account : ${ACCOUNT:-<DEPLOYER_PK>}  ($DEPLOYER_ADDR)"
echo "  rpc     : $(mask "$RPC_EFFECTIVE")"
echo "  agents  : $AGENTS   symbol: $SYMBOL (scored on live Pyth)   cycles: $CYCLES"
echo "========================================================"
sleep 2

# --- (1) Deploy the stack (or reuse) -----------------------------------------
# We need BOTH Colosseum (the live-duel backbone) and MemoryAnchor (the
# identity-bound memory-root anchor). Deploy once and parse both, or reuse via
# --colosseum / --memory-anchor.
if [[ -z "$COLOSSEUM" || -z "$MEMORY_ANCHOR" ]]; then
  echo "[1/2] deploying the stack (forge script Deploy) to get Colosseum + MemoryAnchor ..."
  RUN_LOG="$REPO_ROOT/deployments/arena.deploy.log"
  mkdir -p "$REPO_ROOT/deployments"
  (
    cd "$CONTRACTS_DIR" && \
    PRIVATE_KEY="$PK_RESOLVED" forge script "script/Deploy.s.sol:Deploy" \
      --rpc-url "$RPC_EFFECTIVE" --broadcast --slow
  ) 2>&1 | sed -E 's#(swrm_)[A-Za-z0-9]+#\1<redacted>#g' | tee "$RUN_LOG"
  if [[ -z "$COLOSSEUM" ]]; then
    COLOSSEUM="$(grep -E "^[[:space:]]*Colosseum[[:space:]]*:[[:space:]]+0x[0-9a-fA-F]{40}" "$RUN_LOG" | tail -1 | awk '{print $NF}')"
  fi
  if [[ -z "$MEMORY_ANCHOR" ]]; then
    MEMORY_ANCHOR="$(grep -E "^[[:space:]]*MemoryAnchor[[:space:]]*:[[:space:]]+0x[0-9a-fA-F]{40}" "$RUN_LOG" | tail -1 | awk '{print $NF}')"
  fi
  [[ -n "$COLOSSEUM" ]] || { echo "could not parse Colosseum address from forge output (see $RUN_LOG)" >&2; exit 5; }
  [[ -n "$MEMORY_ANCHOR" ]] || { echo "could not parse MemoryAnchor address from forge output (see $RUN_LOG)" >&2; exit 5; }
fi
echo "  Colosseum   : $COLOSSEUM"
echo "  MemoryAnchor: $MEMORY_ANCHOR"
echo
echo "UI wiring — set in web/apps/web/.env.local then restart the dev server:"
echo "    NEXT_PUBLIC_COLOSSEUM=$COLOSSEUM"
echo "    explorer: https://testnet.arcscan.app/address/$COLOSSEUM"

# --- (1b) Optional: lower the registration stake for a faucet budget ----------
# Set STAKE_USDC in .env (e.g. 1) so each agent's bond is small; otherwise the
# contract default applies and the operator needs a much larger USDC balance.
if [[ -n "${STAKE_USDC:-}" ]]; then
  echo "setting stakeRequirement = ${STAKE_USDC} USDC ..."
  COLOSSEUM_ADDR="$COLOSSEUM" RPC_EFFECTIVE="$RPC_EFFECTIVE" PK_RESOLVED="$PK_RESOLVED" STAKE_USDC="$STAKE_USDC" "$PY" - <<'PYEOF'
import os, sys
sys.path.insert(0, os.environ["REPO_ROOT"])
from eth_abi import encode
from eth_utils import keccak
from scripts.lib.chain import cast_send, wait_for_receipt
units = int(round(float(os.environ["STAKE_USDC"]) * 1_000_000))
sel = keccak(b"setStakeRequirement(uint256)")[:4]
data = "0x" + (sel + encode(["uint256"], [units])).hex()
tx = cast_send(rpc_url=os.environ["RPC_EFFECTIVE"], pk=os.environ["PK_RESOLVED"],
               to=os.environ["COLOSSEUM_ADDR"], data=data)
wait_for_receipt(os.environ["RPC_EFFECTIVE"], tx, timeout=90.0)
print(f"  stakeRequirement set to {units} base units")
PYEOF
fi

# --- (1c) Ensure a keystore password so agent keypairs can be encrypted -------
# Agents are ephemeral (the run uses in-memory keys); this password only encrypts
# the on-disk keystores. Auto-generate + persist one to .env (gitignored) so the
# keystores stay decryptable later. Zero operator action.
if [[ -z "${ARENA_KEY_PASSWORD:-}" && -z "${KEYSTORE_PASSWORD:-}" ]]; then
  ARENA_KEY_PASSWORD="$("$PY" -c "import secrets; print(secrets.token_urlsafe(24))")"
  export ARENA_KEY_PASSWORD
  printf '\n# auto-generated password encrypting the arena agent keystores\nARENA_KEY_PASSWORD=%s\n' "$ARENA_KEY_PASSWORD" >> "$REPO_ROOT/.env"
  echo "generated ARENA_KEY_PASSWORD and saved it to .env (encrypts agent keystores)"
  echo "  note: .env now holds a key-equivalent secret — keep it private (it is gitignored)."
fi

# --- (2) Run the live arena: spawn -> provision -> assemble -> run -----------
# The operator key is passed in the ENV (never argv). The agent-keystore password
# (ARENA_KEY_PASSWORD / KEYSTORE_PASSWORD) is inherited from the exported env.
# STAKE_USDC, if set, flows to the launcher as the per-agent stake.
STAKE_ARG=()
[[ -n "${STAKE_USDC:-}" ]] && STAKE_ARG=(--stake-usdc "$STAKE_USDC")
DUR_ARG=()
[[ -n "$DURATION" ]] && DUR_ARG=(--duration "$DURATION")
echo
echo "[2/2] launching the arena: $AGENTS agents, $CYCLES cycles/duel on $SYMBOL ..."
cd "$REPO_ROOT"
exec env DEPLOYER_PK="$PK_RESOLVED" ARENA_RPC_URL="$RPC_EFFECTIVE" \
  "$PY" -m scripts.run_arena \
    --colosseum "$COLOSSEUM" \
    --memory-anchor "$MEMORY_ANCHOR" \
    --agents "$AGENTS" \
    --cycles "$CYCLES" \
    --symbol "$SYMBOL" \
    --rpc-url "$RPC_EFFECTIVE" \
    "${STAKE_ARG[@]}" \
    "${DUR_ARG[@]}"
