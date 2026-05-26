#!/usr/bin/env bash
# arena_forever.sh — keep a LIVE duel always available in The Arena on Arc.
#
# A supervised forever-loop on top of scripts.run_arena. It provisions an agent
# pool ONCE (or reuses an existing one), then loops indefinitely: create + run a
# fresh round of duels (default --duration 300s) on the SAME pre-provisioned
# agents, resolve, and IMMEDIATELY start the next round. Because rounds 2..N
# REUSE the already-minted/registered/staked pool (--reuse-keystores), each gap
# between rounds is just the create->mine latency — no 60-90s spawn+fund+register
# burn, no extra USDC/gas per round.
#
# Each round is wrapped so a TRANSIENT failure (RPC blip, a reverted tx, a model
# timeout) restarts the loop instead of killing it — a while-true supervisor with
# a short backoff. Ctrl-C (SIGINT/SIGTERM) stops cleanly.
#
# Targets the deployed stack by default:
#   Colosseum    0x03D0cD31a5FA5f7E10259782974c46712548D11c
#   MemoryAnchor 0xB0230ce8940925d719f972d9f00bC6572E220f1E
#
# Needs a model provider key — $OPENROUTER_API_KEY (OpenAI-compatible, routes to
# Claude/any model) or $ANTHROPIC_API_KEY. The duelists make real model calls —
# never faked. Broadcasts real transactions + spends faucet USDC (one-time pool
# provision, then per-round createDuel/cycles/resolve gas). It REFUSES without
# --yes-i-understand.
#
# Usage:
#   bash scripts/arena_forever.sh                                  # refuses (safe)
#   bash scripts/arena_forever.sh --yes-i-understand
#   bash scripts/arena_forever.sh --reuse-keystores agents/.arena_keystore \
#        --duration 300 --yes-i-understand
#
# Flags:
#   --account <name>        Foundry keystore (preferred; key never on argv). Or set DEPLOYER_PK.
#   --colosseum <addr>      Colosseum to use (default: deployed address above).
#   --memory-anchor <addr>  MemoryAnchor to use (default: deployed address above).
#   --reuse-keystores <dir> Reuse an ALREADY-provisioned pool in <dir> (skip the
#                           one-time provision). Default: agents/.arena_keystore.
#   --agents <n>            Agents to provision on first run (default 4).
#   --cycles <n>            Scored cycles per duel (default 4).
#   --duration <secs>       Trading-window seconds per round (default 300).
#   --symbol <SYM>          Pyth-scored asset (default SOL).
#   --rpc-url <url>         Override $RPC / $ARENA_RPC_URL.
#   --backoff <secs>        Sleep after a failed round before retrying (default 10).
#   --yes-i-understand,-y   REQUIRED — confirms real USDC gas + stake spend.
#   --help,-h               Show this help.
set -euo pipefail

# Deployed stack (Arc testnet 5042002) — the always-live targets.
DEFAULT_COLOSSEUM="0x03D0cD31a5FA5f7E10259782974c46712548D11c"
DEFAULT_MEMORY_ANCHOR="0xB0230ce8940925d719f972d9f00bC6572E220f1E"

ACCOUNT=""; YES=0
COLOSSEUM="$DEFAULT_COLOSSEUM"; MEMORY_ANCHOR="$DEFAULT_MEMORY_ANCHOR"
REUSE_DIR=""; AGENTS=4; CYCLES=4; DURATION=300; SYMBOL="SOL"; RPC_FLAG=""; BACKOFF=10
while [[ $# -gt 0 ]]; do
  case "$1" in
    --account) ACCOUNT="$2"; shift 2 ;;
    --account=*) ACCOUNT="${1#*=}"; shift ;;
    --colosseum) COLOSSEUM="$2"; shift 2 ;;
    --colosseum=*) COLOSSEUM="${1#*=}"; shift ;;
    --memory-anchor) MEMORY_ANCHOR="$2"; shift 2 ;;
    --memory-anchor=*) MEMORY_ANCHOR="${1#*=}"; shift ;;
    --reuse-keystores|--pool) REUSE_DIR="$2"; shift 2 ;;
    --reuse-keystores=*|--pool=*) REUSE_DIR="${1#*=}"; shift ;;
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
    --backoff) BACKOFF="$2"; shift 2 ;;
    --backoff=*) BACKOFF="${1#*=}"; shift ;;
    --yes-i-understand|-y) YES=1; shift ;;
    -h|--help) sed -n '1,48p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "arena_forever.sh: unknown arg: $1" >&2; exit 2 ;;
  esac
done

# --- Safety gate FIRST: refuse before any env/RPC/broadcast work -------------
if [[ "$YES" -ne 1 ]]; then
  cat >&2 <<'REFUSE'
REFUSING: arena_forever.sh broadcasts REAL transactions to Arc testnet and spends
faucet USDC — a one-time pool provision (per-agent fund/identity/register) then an
ENDLESS loop of rounds (createDuel + scored cycles + resolve, each spending gas).
The duelists also make REAL model calls. Re-run with --yes-i-understand once the
operator is funded. Example:

  bash scripts/arena_forever.sh --yes-i-understand

No transaction was sent.
REFUSE
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PY="$REPO_ROOT/agents/.venv/bin/python"; [[ -x "$PY" ]] || PY="python3"
export REPO_ROOT

# Default pool dir matches agent_wallet.DEFAULT_KEYSTORE_DIR so the one-time
# provision writes where a later reuse reads.
REUSE_DIR="${REUSE_DIR:-$REPO_ROOT/agents/.arena_keystore}"

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
RPC_EFFECTIVE="${RPC_FLAG:-${ARENA_RPC_URL:-${RPC:-}}}"
[[ -n "$RPC_EFFECTIVE" ]] || { echo "need \$RPC / \$ARENA_RPC_URL or --rpc-url" >&2; exit 3; }
if [[ -z "$ACCOUNT" && -z "${DEPLOYER_PK:-}" ]]; then
  echo "need a signer: --account <keystore> or \$DEPLOYER_PK" >&2; exit 3
fi
if [[ -z "${OPENROUTER_API_KEY:-}" && -z "${ANTHROPIC_API_KEY:-}" ]]; then
  echo "need \$OPENROUTER_API_KEY (routes to Claude/any model) or \$ANTHROPIC_API_KEY" >&2
  echo "— the duelists make REAL model calls (never faked)." >&2
  exit 3
fi

# Redact provider/RPC secrets in any echoed line (mirrors arena_live.sh).
mask() { echo "$1" | sed -E 's#(swrm_)[A-Za-z0-9]+#\1<redacted>#g'; }

# Resolve the deployer key in-process (never on argv). Reused as the launcher's
# operator DEPLOYER_PK.
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

# Ensure a keystore password so the one-time provision can encrypt the pool's
# keystores AND so later reuse can decrypt them. Auto-generate + persist to .env
# (gitignored) when absent — zero operator action.
if [[ -z "${ARENA_KEY_PASSWORD:-}" && -z "${KEYSTORE_PASSWORD:-}" ]]; then
  ARENA_KEY_PASSWORD="$("$PY" -c "import secrets; print(secrets.token_urlsafe(24))")"
  export ARENA_KEY_PASSWORD
  printf '\n# auto-generated password encrypting the arena agent keystores\nARENA_KEY_PASSWORD=%s\n' "$ARENA_KEY_PASSWORD" >> "$REPO_ROOT/.env"
  echo "generated ARENA_KEY_PASSWORD and saved it to .env (encrypts agent keystores)"
  echo "  note: .env now holds a key-equivalent secret — keep it private (it is gitignored)."
fi

# Does the pool dir already hold a provisioned pool (keystores + identities)?
pool_ready() {
  [[ -f "$REUSE_DIR/identities.json" ]] && \
    compgen -G "$REUSE_DIR/*.keystore" >/dev/null 2>&1
}

echo "============ The Arena — ALWAYS LIVE on Arc ============="
echo "  account : ${ACCOUNT:-<DEPLOYER_PK>}  ($DEPLOYER_ADDR)"
echo "  rpc     : $(mask "$RPC_EFFECTIVE")"
echo "  Colosseum   : $COLOSSEUM"
echo "  MemoryAnchor: $MEMORY_ANCHOR"
echo "  pool dir    : $REUSE_DIR"
echo "  symbol: $SYMBOL  cycles: $CYCLES  duration: ${DURATION}s/round  backoff: ${BACKOFF}s"
echo "========================================================"

STAKE_ARG=()
[[ -n "${STAKE_USDC:-}" ]] && STAKE_ARG=(--stake-usdc "$STAKE_USDC")

cd "$REPO_ROOT"

# Run ONE round of the launcher. The first call provisions the pool fresh (and
# run_arena persists identities.json so the pool becomes reusable); every later
# call reuses it. Returns the launcher's exit code.
run_round() {
  local reuse_arg=()
  if pool_ready; then
    reuse_arg=(--reuse-keystores "$REUSE_DIR")
  else
    echo ">> no provisioned pool in $REUSE_DIR — provisioning a fresh pool (one-time) ..."
  fi
  env DEPLOYER_PK="$PK_RESOLVED" ARENA_RPC_URL="$RPC_EFFECTIVE" \
      ARENA_KEY_PASSWORD="${ARENA_KEY_PASSWORD:-${KEYSTORE_PASSWORD:-}}" \
    "$PY" -m scripts.run_arena \
      --colosseum "$COLOSSEUM" \
      --memory-anchor "$MEMORY_ANCHOR" \
      --agents "$AGENTS" \
      --cycles "$CYCLES" \
      --symbol "$SYMBOL" \
      --duration "$DURATION" \
      --rpc-url "$RPC_EFFECTIVE" \
      "${reuse_arg[@]}" \
      "${STAKE_ARG[@]}" 2>&1 | sed -E 's#(swrm_)[A-Za-z0-9]+#\1<redacted>#g'
  # Propagate the launcher's exit status, not sed's.
  return "${PIPESTATUS[0]}"
}

# Clean shutdown on Ctrl-C / TERM.
RUNNING=1
trap 'RUNNING=0; echo; echo "arena_forever.sh: stopping after the current round ..."' INT TERM

# --- Supervised forever-loop -------------------------------------------------
# A transient failure (RPC blip, reverted tx, model timeout) must restart the
# loop, not kill it: each round runs in a subshell guarded by `|| true`, then we
# back off briefly on failure before the next round. Successful rounds start the
# next IMMEDIATELY (gap = create->mine latency only).
ROUND=0
while [[ "$RUNNING" -eq 1 ]]; do
  ROUND=$((ROUND + 1))
  echo
  echo ">>> ROUND $ROUND  ($(date -u '+%Y-%m-%dT%H:%M:%SZ'))  duration=${DURATION}s"
  if run_round; then
    echo ">>> ROUND $ROUND complete — starting the next immediately."
  else
    rc=$?
    echo ">>> ROUND $ROUND failed (exit $rc) — supervisor backing off ${BACKOFF}s then retrying." >&2
    # Interruptible backoff so Ctrl-C during the wait still exits promptly.
    for _ in $(seq 1 "$BACKOFF"); do [[ "$RUNNING" -eq 1 ]] || break; sleep 1; done
  fi
done

echo "arena_forever.sh: supervisor stopped after $ROUND round(s)."
