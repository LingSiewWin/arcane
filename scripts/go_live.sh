#!/usr/bin/env bash
# One-shot live broadcast launcher (writes a REAL scripts/demo_output.jsonl).
# Signer (pick one):
#   * DEPLOYER_PK env  — no keystore/password:
#       DEPLOYER_PK=0x<key> bash scripts/go_live.sh
#   * Foundry keystore — pass the account name as $1:
#       bash scripts/go_live.sh arc-deployer
set -euo pipefail

# Load $RPC (Arc testnet) from the canteen env if present.
if [ -f "$HOME/.arc-canteen/env" ]; then
  # shellcheck disable=SC1091
  . "$HOME/.arc-canteen/env"
fi

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

echo "RPC     : ${RPC:0:42}…"
echo "Launching LIVE demo against Arc testnet — this broadcasts real txs."
echo

# Prefer the DEPLOYER_PK env path (no keystore password). Only fall back to a
# keystore --account when DEPLOYER_PK is unset.
if [ -n "${DEPLOYER_PK:-}" ]; then
  echo "Signer  : DEPLOYER_PK (env)"
  echo
  exec agents/.venv/bin/python -m scripts.demo_e2e --mode live --yes-i-understand
else
  ACCOUNT="${1:-arc-deployer}"
  echo "Signer  : keystore '$ACCOUNT'"
  echo
  exec agents/.venv/bin/python -m scripts.demo_e2e \
    --mode live --account "$ACCOUNT" --yes-i-understand
fi
