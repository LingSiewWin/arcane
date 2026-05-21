#!/usr/bin/env bash
# deploy_arc.sh — bash wrapper around `forge script` that deploys the four
# Constrained-Cognition contracts (ConstitutionRegistry, ConstitutionHook,
# MemoryAnchor, BondVault) to Arc testnet.
#
# Defaults to DRY-RUN: prints what would be deployed but never broadcasts.
# Broadcasting requires BOTH `--broadcast` AND a non-empty $DEPLOYER_PK in env.
#
# Usage:
#   scripts/deploy_arc.sh                  # dry-run, prints plan
#   scripts/deploy_arc.sh --broadcast      # broadcast to Arc testnet
#   scripts/deploy_arc.sh --rpc-url URL    # override RPC (default: $RPC or ~/.arc-canteen/env)
#
# Env (sourced from ~/.arc-canteen/env if present, then overlaid by current shell):
#   RPC               required for broadcast. Arc testnet RPC URL.
#   DEPLOYER_PK       required for broadcast. 0x-prefixed private key, must be
#                     funded with USDC on Arc testnet (faucet.circle.com).
#   ARC_USDC          optional. Defaults to 0x3600...0000.
#   BOND_ORACLE       optional. Defaults to deployer.
#   BOND_INSURANCE    optional. Defaults to deployer.
#   BOND_WINDOW_SECS  optional. Defaults to 604800 (7d).
#
# Output:
#   On success (broadcast), writes deployments/arc-testnet.json with the four
#   contract addresses + the deployer + chain id + a UTC timestamp.

set -euo pipefail

# Locate repo root via this script's location so it works from any cwd.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
DEPLOYMENTS_DIR="$REPO_ROOT/deployments"
CONTRACTS_DIR="$REPO_ROOT/contracts"
ENV_FILE="$HOME/.arc-canteen/env"

# Source the canteen env if available — gives us $RPC without baking the token
# into anything tracked. Tolerate absence so dry-runs work in fresh checkouts.
if [[ -f "$ENV_FILE" ]]; then
    # shellcheck disable=SC1090
    . "$ENV_FILE"
fi

BROADCAST=0
RPC_URL_FLAG=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --broadcast)
            BROADCAST=1
            shift
            ;;
        --rpc-url)
            RPC_URL_FLAG="$2"
            shift 2
            ;;
        --rpc-url=*)
            RPC_URL_FLAG="${1#*=}"
            shift
            ;;
        -h|--help)
            sed -n '1,40p' "${BASH_SOURCE[0]}"
            exit 0
            ;;
        *)
            echo "deploy_arc.sh: unknown arg: $1" >&2
            exit 2
            ;;
    esac
done

# Effective RPC: --rpc-url > $RPC.
RPC_EFFECTIVE="${RPC_URL_FLAG:-${RPC:-}}"

# Mask the RPC token for display — never echo secrets to the terminal.
mask_rpc() {
    local url="$1"
    if [[ -z "$url" ]]; then
        echo "<unset>"
        return
    fi
    # Replace any "swrm_<hex>" or last path segment of token-like length with a placeholder.
    echo "$url" | sed -E 's#(swrm_)[A-Za-z0-9]+#\1<redacted>#g'
}

if [[ "$BROADCAST" -eq 1 ]]; then BROADCAST_LABEL="YES"; else BROADCAST_LABEL="NO (dry-run)"; fi

# Dry-run preamble — always print the plan so the user can see what WOULD happen.
echo "deploy_arc.sh plan:"
echo "  contracts dir : $CONTRACTS_DIR"
echo "  script        : script/Deploy.s.sol:Deploy"
echo "  contracts that will be deployed:"
echo "    1. ConstitutionRegistry"
echo "    2. ConstitutionHook    (registry: -> 1)"
echo "    3. MemoryAnchor"
echo "    4. BondVault           (token: \${ARC_USDC:-0x3600...0000})"
echo "  rpc           : $(mask_rpc "$RPC_EFFECTIVE")"
echo "  broadcast     : $BROADCAST_LABEL"

if [[ "$BROADCAST" -ne 1 ]]; then
    echo
    echo "Dry-run complete. Re-run with --broadcast to send real transactions."
    exit 0
fi

# --- Broadcast path ----------------------------------------------------------

if [[ -z "${DEPLOYER_PK:-}" ]]; then
    echo "deploy_arc.sh: --broadcast requires DEPLOYER_PK env var (0x-prefixed)" >&2
    exit 3
fi
if [[ -z "$RPC_EFFECTIVE" ]]; then
    echo "deploy_arc.sh: --broadcast requires RPC. Set \$RPC or pass --rpc-url." >&2
    exit 3
fi

if ! command -v forge >/dev/null 2>&1; then
    echo "deploy_arc.sh: 'forge' not found on PATH. Install Foundry first." >&2
    exit 4
fi

mkdir -p "$DEPLOYMENTS_DIR"
RUN_LOG="$DEPLOYMENTS_DIR/arc-testnet.last-run.log"

echo
echo "broadcasting to $(mask_rpc "$RPC_EFFECTIVE") ..."

# Run forge script. We pass DEPLOYER_PK via env (not --private-key flag) so it
# doesn't leak via `ps auxww` / /proc/<pid>/cmdline. Forge picks up
# PRIVATE_KEY automatically when present.
PRIVATE_KEY="$DEPLOYER_PK" forge script \
    "script/Deploy.s.sol:Deploy" \
    --root "$CONTRACTS_DIR" \
    --rpc-url "$RPC_EFFECTIVE" \
    --broadcast \
    --slow \
    --silent 2>&1 | tee "$RUN_LOG"

# Parse the four addresses out of forge's logs. The Deploy script uses
# `console2.log("ConstitutionRegistry:", address)` — forge prints these as
# `  ConstitutionRegistry: 0x...`.
extract() {
    grep -E "^[[:space:]]*$1:[[:space:]]+0x[0-9a-fA-F]{40}" "$RUN_LOG" \
        | tail -1 \
        | awk '{print $NF}'
}

REG_ADDR="$(extract 'ConstitutionRegistry' || true)"
HOOK_ADDR="$(extract 'ConstitutionHook' || true)"
ANCHOR_ADDR="$(extract 'MemoryAnchor' || true)"
VAULT_ADDR="$(extract 'BondVault' || true)"

if [[ -z "$REG_ADDR" || -z "$HOOK_ADDR" || -z "$ANCHOR_ADDR" || -z "$VAULT_ADDR" ]]; then
    echo "deploy_arc.sh: could not parse all four addresses from forge output." >&2
    echo "see $RUN_LOG for details." >&2
    exit 5
fi

# Chain id (cheap separate call, gives us provenance).
CHAIN_ID="$(cast chain-id --rpc-url "$RPC_EFFECTIVE" 2>/dev/null || echo "unknown")"

# Deployer address from the broadcast deployer's PK.
#
# SECURITY: `cast wallet address --private-key $PK` leaks the key via argv
# (visible to anyone with ps access for the lifetime of the subprocess).
# Derive the address in pure Python via eth_account instead — the key
# stays in the parent shell's env and never crosses argv.
DEPLOYER_ADDR="$(
    DEPLOYER_PK="$DEPLOYER_PK" python3 -c '
import os, sys
try:
    from eth_account import Account
    print(Account.from_key(os.environ["DEPLOYER_PK"]).address)
except Exception:
    sys.exit(1)
' 2>/dev/null || echo "unknown"
)"

OUT_FILE="$DEPLOYMENTS_DIR/arc-testnet.json"
TIMESTAMP="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

cat > "$OUT_FILE" <<EOF
{
  "chain": "arc-testnet",
  "chainId": $CHAIN_ID,
  "deployedAt": "$TIMESTAMP",
  "deployer": "$DEPLOYER_ADDR",
  "contracts": {
    "ConstitutionRegistry": "$REG_ADDR",
    "ConstitutionHook": "$HOOK_ADDR",
    "MemoryAnchor": "$ANCHOR_ADDR",
    "BondVault": "$VAULT_ADDR"
  },
  "config": {
    "ARC_USDC": "${ARC_USDC:-0x3600000000000000000000000000000000000000}",
    "BOND_ORACLE": "${BOND_ORACLE:-$DEPLOYER_ADDR}",
    "BOND_INSURANCE": "${BOND_INSURANCE:-$DEPLOYER_ADDR}",
    "BOND_WINDOW_SECS": ${BOND_WINDOW_SECS:-604800}
  }
}
EOF

echo
echo "wrote $OUT_FILE"
echo
echo "  ConstitutionRegistry: $REG_ADDR"
echo "  ConstitutionHook    : $HOOK_ADDR"
echo "  MemoryAnchor        : $ANCHOR_ADDR"
echo "  BondVault           : $VAULT_ADDR"
