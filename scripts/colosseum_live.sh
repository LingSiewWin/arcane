#!/usr/bin/env bash
# colosseum_live.sh — bring The Colosseum up on REAL Arc testnet in one command:
# (re)use or deploy the Colosseum contract, approve + register/stake both agents,
# then run a live duel where two REAL LLM duelists (Anthropic) make the calls,
# scored on REAL Pyth prices (free Hermes), emitting on-chain duel actions the
# /colosseum UI reads live. At resolve it pays the dual prizes (Alpha + Iron
# Shield) and refunds stakes.
#
# Needs a model provider key — $OPENROUTER_API_KEY (OpenAI-compatible, routes to
# Claude/any model) or $ANTHROPIC_API_KEY. The duelists make real model calls —
# never faked. Optionally set $OPENROUTER_MODEL (e.g. anthropic/claude-3.5-haiku)
# or pass --model. Broadcasts real transactions + spends faucet USDC for gas +
# stakes. It REFUSES without --yes-i-understand.
#
# Usage:
#   bash scripts/colosseum_live.sh                                  # refuses (safe)
#   bash scripts/colosseum_live.sh --account arc-deployer --yes-i-understand
#   bash scripts/colosseum_live.sh --colosseum 0x... --yes-i-understand \
#        --agent-a 0xAAA... --agent-b 0xBBB... --cycles 6 --symbol SOL
#
# Flags:
#   --account <name>     Foundry keystore (preferred; key never on argv). Or set DEPLOYER_PK.
#   --colosseum <addr>   Reuse an already-deployed Colosseum (skip deploy).
#   --agent-a <addr>     Hardened gladiator (default: the deployer address).
#   --agent-b <addr>     Naive gladiator (default: 0x..bEEF demo address).
#   --cycles <n>         Scored cycles (default 6).
#   --symbol <SYM>       Pyth-scored asset (default SOL).
#   --rpc-url <url>      Override $RPC.
#   --yes-i-understand   REQUIRED — confirms real USDC gas spend.
set -euo pipefail

ACCOUNT=""; YES=0; COLOSSEUM=""; AGENT_A=""; AGENT_B=""; CYCLES=6; SYMBOL="SOL"; RPC_FLAG=""; DURATION=180
while [[ $# -gt 0 ]]; do
  case "$1" in
    --account) ACCOUNT="$2"; shift 2 ;;
    --colosseum) COLOSSEUM="$2"; shift 2 ;;
    --agent-a) AGENT_A="$2"; shift 2 ;;
    --agent-b) AGENT_B="$2"; shift 2 ;;
    --cycles) CYCLES="$2"; shift 2 ;;
    --symbol) SYMBOL="$2"; shift 2 ;;
    --duration) DURATION="$2"; shift 2 ;;
    --rpc-url) RPC_FLAG="$2"; shift 2 ;;
    --yes-i-understand|-y) YES=1; shift ;;
    -h|--help) sed -n '1,30p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "colosseum_live.sh: unknown arg: $1" >&2; exit 2 ;;
  esac
done

if [[ "$YES" -ne 1 ]]; then
  cat >&2 <<'REFUSE'
REFUSING: colosseum_live.sh broadcasts REAL transactions to Arc testnet and
spends faucet USDC for gas (Colosseum deploy + createDuel + scored cycles).
Re-run with --yes-i-understand once the operator is funded. Example:

  bash scripts/colosseum_live.sh --account arc-deployer --yes-i-understand

No transaction was sent.
REFUSE
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
CONTRACTS_DIR="$REPO_ROOT/contracts"
PY="$REPO_ROOT/agents/.venv/bin/python"; [[ -x "$PY" ]] || PY="python3"
export REPO_ROOT

# Load root .env (gitignored) for backend keys/RPC — no `export` needed. Then
# the canteen env (for RPC) if present. Already-exported shell vars still win
# because we only fill ones that are currently empty.
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
[[ -f "$HOME/.arc-canteen/env" ]] && . "$HOME/.arc-canteen/env"
# Account can come from .env (DEPLOYER_ACCOUNT) so the command line stays short;
# an explicit --account flag still wins.
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
# the duel runner's DEPLOYER_PK.
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

echo "================ The Colosseum — LIVE on Arc ================"
echo "  account : ${ACCOUNT:-<DEPLOYER_PK>}  ($DEPLOYER_ADDR)"
echo "  rpc     : $(mask "$RPC_EFFECTIVE")"
echo "  symbol  : $SYMBOL (scored on live Pyth)"
echo "============================================================"
sleep 2

# --- (1) Deploy Colosseum (or reuse) -----------------------------------------
if [[ -z "$COLOSSEUM" ]]; then
  echo "[1/3] deploying the stack (forge script Deploy) to get a fresh Colosseum ..."
  RUN_LOG="$REPO_ROOT/deployments/colosseum.deploy.log"
  mkdir -p "$REPO_ROOT/deployments"
  (
    cd "$CONTRACTS_DIR" && \
    PRIVATE_KEY="$PK_RESOLVED" forge script "script/Deploy.s.sol:Deploy" \
      --rpc-url "$RPC_EFFECTIVE" --broadcast --slow
  ) 2>&1 | sed -E 's#(swrm_)[A-Za-z0-9]+#\1<redacted>#g' | tee "$RUN_LOG"
  COLOSSEUM="$(grep -E "^[[:space:]]*Colosseum[[:space:]]*:[[:space:]]+0x[0-9a-fA-F]{40}" "$RUN_LOG" | tail -1 | awk '{print $NF}')"
  [[ -n "$COLOSSEUM" ]] || { echo "could not parse Colosseum address from forge output (see $RUN_LOG)" >&2; exit 5; }
fi
echo "  Colosseum: $COLOSSEUM"

AGENT_A="${AGENT_A:-$DEPLOYER_ADDR}"
AGENT_B="${AGENT_B:-0x000000000000000000000000000000000000bEEF}"
echo
echo "UI wiring — set in web/apps/web/.env.local then restart the dev server:"
echo "    NEXT_PUBLIC_COLOSSEUM=$COLOSSEUM"
echo "    explorer: https://testnet.arcscan.app/address/$COLOSSEUM"

# --- (2) Approve USDC so registration stakes + injection fees can pull --------
echo
echo "approving USDC -> Colosseum (covers the agents' registration stakes) ..."
COLOSSEUM_ADDR="$COLOSSEUM" RPC_EFFECTIVE="$RPC_EFFECTIVE" PK_RESOLVED="$PK_RESOLVED" "$PY" - <<'PYEOF'
import os, sys
sys.path.insert(0, os.environ["REPO_ROOT"])
from eth_abi import encode
from eth_utils import keccak, to_canonical_address
from scripts.lib.chain import cast_send, wait_for_receipt
usdc = "0x3600000000000000000000000000000000000000"  # Arc native USDC (ERC-20)
sel = keccak(b"approve(address,uint256)")[:4]
data = "0x" + (sel + encode(
    ["address", "uint256"],
    [to_canonical_address(os.environ["COLOSSEUM_ADDR"]), (1 << 256) - 1],
)).hex()
tx = cast_send(rpc_url=os.environ["RPC_EFFECTIVE"], pk=os.environ["PK_RESOLVED"], to=usdc, data=data)
wait_for_receipt(os.environ["RPC_EFFECTIVE"], tx, timeout=90.0)
print("  USDC approved")
PYEOF

# --- (2b) Optional: lower the registration stake for a faucet budget ----------
# Set STAKE_USDC in .env (e.g. 1) so each agent's bond is small; otherwise the
# contract default (50 USDC) applies and the operator needs >=100 USDC.
if [[ -n "${STAKE_USDC:-}" ]]; then
  echo "setting stakeRequirement = ${STAKE_USDC} USDC ..."
  COLOSSEUM_ADDR="$COLOSSEUM" RPC_EFFECTIVE="$RPC_EFFECTIVE" PK_RESOLVED="$PK_RESOLVED" STAKE_USDC="$STAKE_USDC" "$PY" - <<'PYEOF'
import os, sys
sys.path.insert(0, os.environ["REPO_ROOT"])
from eth_abi import encode
from eth_utils import keccak, to_canonical_address
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

# --- (3) Run the live duel: register + stake, real LLM agents, Pyth scoring ---
echo
echo "[3/3] running a live duel: A(hardened)=$AGENT_A  B(naive)=$AGENT_B  cycles=$CYCLES"
cd "$REPO_ROOT"
exec env DEPLOYER_PK="$PK_RESOLVED" ARENA_RPC_URL="$RPC_EFFECTIVE" \
  "$PY" -m agents.duel_runner \
    --colosseum "$COLOSSEUM" \
    --agent-a "$AGENT_A" \
    --agent-b "$AGENT_B" \
    --rpc-url "$RPC_EFFECTIVE" \
    --cycles "$CYCLES" \
    --symbol "$SYMBOL" \
    --duration "$DURATION" \
    --register \
    --resolve
