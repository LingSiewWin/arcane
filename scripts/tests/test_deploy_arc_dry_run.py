"""deploy_arc.sh dry-run smoke test.

Running deploy_arc.sh without --broadcast must exit 0 and list the four
contracts it would deploy. This is the safety gate ensuring nobody can
accidentally send 4 deploys to Arc just by running the script.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEPLOY_SCRIPT = REPO_ROOT / "scripts" / "deploy_arc.sh"


def test_dry_run_prints_plan():
    result = subprocess.run(
        ["bash", str(DEPLOY_SCRIPT)],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, (
        f"dry run should exit 0, got {result.returncode}.\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    out = result.stdout
    # All four contracts must be named in the plan.
    for name in (
        "ConstitutionRegistry",
        "ConstitutionHook",
        "MemoryAnchor",
        "BondVault",
    ):
        assert name in out, f"{name} missing from dry-run output"
    assert "dry-run" in out.lower(), "dry-run output must explicitly say so"


def test_broadcast_without_pk_fails_clean():
    """Asking for --broadcast without DEPLOYER_PK must exit non-zero with a
    clear message — and must NOT actually fire any forge command."""
    env = {}
    # Source ~/.arc-canteen/env would auto-populate RPC, but DEPLOYER_PK should
    # be absent. Make the test environment hermetic by clearing both.
    result = subprocess.run(
        ["bash", str(DEPLOY_SCRIPT), "--broadcast"],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin", "HOME": "/tmp"},
        timeout=20,
    )
    assert result.returncode != 0
    assert "DEPLOYER_PK" in (result.stdout + result.stderr)
