"""--mode live must refuse to run without --yes-i-understand.

This is the safety gate for accidental USDC burn. The check happens BEFORE
any subprocess starts, before any RPC connection, so it's fast.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEMO_SCRIPT = REPO_ROOT / "scripts" / "demo_e2e.py"


def _venv_python() -> str:
    candidate = REPO_ROOT / "agents" / ".venv" / "bin" / "python"
    if candidate.exists():
        return str(candidate)
    return sys.executable


def test_live_mode_without_confirmation_exits_nonzero(tmp_path):
    py = _venv_python()
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT) + ":" + env.get("PYTHONPATH", "")
    result = subprocess.run(
        [
            py,
            str(DEMO_SCRIPT),
            "--mode",
            "live",
            "--output",
            str(tmp_path / "out.jsonl"),
        ],
        capture_output=True,
        text=True,
        env=env,
        timeout=15,
    )
    assert result.returncode != 0, (
        "live mode without --yes-i-understand must exit non-zero. "
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    combined = (result.stdout + result.stderr).lower()
    assert "refusing" in combined or "yes-i-understand" in combined, (
        "live-mode refusal must mention the safety flag. "
        f"stderr={result.stderr}"
    )


def test_live_mode_with_confirmation_but_no_pk_still_exits_nonzero(tmp_path):
    """Sanity: confirming the flag without PK/RPC must still fail clean."""
    py = _venv_python()
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT) + ":" + env.get("PYTHONPATH", "")
    # Clear any leakage from the parent shell.
    env.pop("DEPLOYER_PK", None)
    env.pop("RPC", None)
    result = subprocess.run(
        [
            py,
            str(DEMO_SCRIPT),
            "--mode",
            "live",
            "--yes-i-understand",
            "--output",
            str(tmp_path / "out.jsonl"),
        ],
        capture_output=True,
        text=True,
        env=env,
        timeout=15,
    )
    assert result.returncode != 0
    combined = (result.stdout + result.stderr).lower()
    assert "deployer_pk" in combined or "rpc" in combined
