"""anchor_memory test — call MemoryAnchor.anchor(root) on an anvil fork
and assert that the MemoryAnchored(address,bytes32,uint256) event fires.

The test:
  1. Spawns its own anvil fork (no Arc broadcast).
  2. Deploys MemoryAnchor via forge create.
  3. Calls anchor_memory(...) with a known root.
  4. Checks the receipt has the right event topic + the event_emitted flag.
"""

from __future__ import annotations

import os
import secrets
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts.anchor_memory import anchor_memory  # noqa: E402
from scripts.lib.chain import deploy_contract_via_cast  # noqa: E402


ANVIL_DEFAULT_KEY = (
    "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
)

pytestmark = pytest.mark.skipif(
    shutil.which("anvil") is None
    or shutil.which("forge") is None
    or shutil.which("cast") is None,
    reason="foundry not on PATH",
)


def _pick_port(start=8700, end=8800) -> int:
    for p in range(start, end):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", p))
                return p
            except OSError:
                continue
    raise RuntimeError("no free port")


@pytest.fixture
def anvil_url():
    port = _pick_port()
    proc = subprocess.Popen(
        [
            "anvil",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--chain-id",
            "5042002",
            "--quiet",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    # Wait for port to accept connections.
    deadline = time.time() + 10.0
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.2)
            try:
                s.connect(("127.0.0.1", port))
                break
            except OSError:
                time.sleep(0.05)
    else:
        proc.terminate()
        raise RuntimeError("anvil failed to start")
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()


def test_anchor_memory_emits_event(anvil_url):
    artifact = str(REPO_ROOT / "contracts" / "out" / "MemoryAnchor.sol" / "MemoryAnchor.json")
    anchor_addr, _ = deploy_contract_via_cast(
        rpc_url=anvil_url,
        pk=ANVIL_DEFAULT_KEY,
        artifact_path=artifact,
    )

    # A non-zero known root — the contract rejects bytes32(0).
    root_hex = "0x" + secrets.token_hex(32)
    result = anchor_memory(
        rpc_url=anvil_url,
        pk=ANVIL_DEFAULT_KEY,
        anchor_address=anchor_addr,
        root_hex=root_hex,
    )

    assert result["status"] == 1, f"anchor tx should succeed, got status={result['status']}"
    assert result["event_emitted"] is True, "MemoryAnchored event must be emitted"
    assert result["root"].lower() == root_hex.lower()
    assert result["anchor"].lower() == anchor_addr.lower()
