#!/usr/bin/env bash
# Start the continuous agent runner against the already-deployed arena.
# Reads the registry/oracle addresses from deployments/arena.json so you
# don't retype them. Needs a signer: DEPLOYER_PK env (or --account passed through).
#
# Usage (after arena_live.sh has deployed + seeded):
#   export DEPLOYER_PK=0x<your key>
#   bash scripts/arena_run.sh
# Optional: bash scripts/arena_run.sh 10   (interval seconds, default 15)
set -euo pipefail

INTERVAL="${1:-15}"
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

[ -f "$HOME/.arc-canteen/env" ] && . "$HOME/.arc-canteen/env"

ARENA_JSON="$REPO_ROOT/deployments/arena.json"
if [ ! -f "$ARENA_JSON" ]; then
  echo "arena_run.sh: $ARENA_JSON not found — run arena_live.sh first." >&2
  exit 1
fi

PY="$REPO_ROOT/agents/.venv/bin/python"
REG="$("$PY" -c "import json;print(json.load(open('$ARENA_JSON'))['registry_addr'])")"
IDS="$("$PY" -c "import json;d=json.load(open('$ARENA_JSON'));print(','.join(str(a['agent_id']) for a in d['agents']))")"
ORACLE="0x374c1c144E192b4Ef91eb25141b8665eAaa73Bb3"

if [ -z "${DEPLOYER_PK:-}" ] && [ -z "${DEPLOYER_ACCOUNT:-}" ]; then
  echo "arena_run.sh: need a signer. Run: export DEPLOYER_PK=0x<your key>" >&2
  exit 3
fi

echo "Continuous runner — registry $REG · agents $IDS · interval ${INTERVAL}s"
echo "Ctrl-C to stop. Each cycle: agents publish advice + emit on-chain AgentAction events."
exec "$PY" -m agents.agent_runner \
  --registry "$REG" \
  --agents "$IDS" \
  --oracle "$ORACLE" \
  --rpc-url "$RPC" \
  --interval "$INTERVAL"
