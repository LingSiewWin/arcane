"""Run demo_e2e --mode local in a subprocess and assert all 6 steps emit ok=True.

This is the most load-bearing test of Slice 5D: it proves the entire
demo flow (deploy, x402 query, constitution revert, memory anchor, child
spawn + bond slash) hangs together against an anvil-forked Arc chain.

The test is slow (~30-60s) — it deploys 4 contracts on anvil and sends a
dozen txs. We mark it slow with a generous pytest timeout if available.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEMO_SCRIPT = REPO_ROOT / "scripts" / "demo_e2e.py"

pytestmark = pytest.mark.skipif(
    shutil.which("anvil") is None or shutil.which("forge") is None or shutil.which("cast") is None,
    reason="foundry (anvil/forge/cast) not on PATH",
)


def _venv_python() -> str:
    candidate = REPO_ROOT / "agents" / ".venv" / "bin" / "python"
    if candidate.exists():
        return str(candidate)
    return sys.executable


def test_local_mode_runs_e2e(tmp_path: Path):
    """Run demo_e2e --mode local and assert 6 ok=True lines in JSONL."""
    output_path = tmp_path / "demo_output.jsonl"
    memory_path = tmp_path / "alice.mem"

    py = _venv_python()
    cmd = [
        py,
        str(DEMO_SCRIPT),
        "--mode",
        "local",
        "--output",
        str(output_path),
        "--memory",
        str(memory_path),
        "--seed-n",
        "20",
    ]
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT) + ":" + env.get("PYTHONPATH", "")
    result = subprocess.run(
        cmd, capture_output=True, text=True, env=env, timeout=300
    )
    assert result.returncode == 0, (
        f"demo_e2e local exited non-zero (rc={result.returncode}).\n"
        f"stdout:\n{result.stdout[-2000:]}\n"
        f"stderr:\n{result.stderr[-2000:]}\n"
    )

    assert output_path.exists(), "demo_output.jsonl was not written"
    lines = [json.loads(l) for l in output_path.read_text().splitlines() if l.strip()]
    assert len(lines) == 6, f"expected 6 jsonl lines, got {len(lines)}"

    for rec in lines:
        assert rec["ok"] is True, (
            f"step {rec['step']} ({rec['name']}) reported ok=False: "
            f"evidence={rec['evidence']}"
        )

    # Step 4 must have a tx hash AND a revert reason mentioning ConstitutionViolation.
    step4 = lines[3]
    assert step4["step"] == 4
    assert step4.get("tx_hash"), "step 4 must produce a tx hash"
    assert step4["evidence"].get("receipt_status") == 0, (
        f"step 4 receipt status should be 0 (revert), "
        f"got {step4['evidence'].get('receipt_status')}"
    )
    assert "ConstitutionViolation" in step4["evidence"].get("revert_reason", "")

    # Step 5 must have anchored the pinned root AND emitted the event.
    step5 = lines[4]
    assert step5["step"] == 5
    assert step5["evidence"].get("event_emitted") is True
    assert step5["evidence"].get("pinned_root_stable") is True
