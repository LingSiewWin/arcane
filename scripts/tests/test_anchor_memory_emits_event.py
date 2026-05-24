"""anchor_memory test — call MemoryAnchor.anchor(uint256,bytes32) on an anvil
fork and assert the F10 identity-bound MemoryAnchored event fires.

Phase 4 audit (B5 / N9): the previous test only covered the legacy
``anchor(bytes32)`` selector — the same selector the demo used to ship
with — so it provided zero coverage of the F10 identity-bound fix. The
test below now:

  1. Spawns its own anvil fork (no Arc broadcast).
  2. Deploys MockERC721 (the same contract used by contracts/test/MemoryAnchor.t.sol).
  3. Mints an identity NFT to the deployer's address.
  4. Deploys MemoryAnchor with the MockERC721 registry.
  5. Calls anchor_memory(..., identity_id=ALICE_ID) — the F10 path.
  6. Asserts MemoryAnchored fires with the EXPECTED identity_id in
     topic[2] (not zero).
  7. Adds a separate legacy-path test that still exercises the
     unprotected ``anchor(bytes32)`` selector for backward-compat.
"""

from __future__ import annotations

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
from scripts.lib.chain import (  # noqa: E402
    cast_address_from_pk,
    cast_send,
    deploy_contract_via_cast,
    wait_for_receipt,
)


ANVIL_DEFAULT_KEY = (
    "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
)
ALICE_IDENTITY_ID = 42

# The MockERC721 in contracts/test/MemoryAnchor.t.sol is compiled as part
# of the forge build; the artifact lands under MemoryAnchor.t.sol/.
MOCK_ERC721_ARTIFACT = (
    REPO_ROOT / "contracts" / "out" / "MemoryAnchor.t.sol" / "MockERC721.json"
)
MEMORY_ANCHOR_ARTIFACT = (
    REPO_ROOT / "contracts" / "out" / "MemoryAnchor.sol" / "MemoryAnchor.json"
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


def _deploy_mock_erc721_and_mint(
    rpc_url: str, pk: str, owner: str, token_id: int
) -> str:
    """Deploy MockERC721 and mint ``token_id`` to ``owner``. Returns address."""
    assert MOCK_ERC721_ARTIFACT.exists(), (
        f"MockERC721 artifact missing: {MOCK_ERC721_ARTIFACT}; run `forge build`"
    )
    registry_addr, _ = deploy_contract_via_cast(
        rpc_url=rpc_url,
        pk=pk,
        artifact_path=str(MOCK_ERC721_ARTIFACT),
    )
    mint_tx = cast_send(
        rpc_url=rpc_url,
        pk=pk,
        to=registry_addr,
        sig="mint(address,uint256)",
        args=[owner, str(token_id)],
    )
    wait_for_receipt(rpc_url, mint_tx, timeout=30)
    return registry_addr


def test_anchor_memory_identity_bound_path_emits_event(anvil_url):
    """F10 path: deploy a MockERC721 registry, mint an identity to the deployer,
    deploy MemoryAnchor pointing at the registry, then anchor via the
    identity-bound entry point. The MemoryAnchored event MUST fire with
    a non-zero identityId in topic[2]."""
    deployer = cast_address_from_pk(ANVIL_DEFAULT_KEY)

    # 1. Stand up a MockERC721 registry and mint identityId=42 to deployer.
    registry_addr = _deploy_mock_erc721_and_mint(
        anvil_url, ANVIL_DEFAULT_KEY, deployer, ALICE_IDENTITY_ID
    )

    # 2. Deploy MemoryAnchor pointing at the registry.
    anchor_addr, _ = deploy_contract_via_cast(
        rpc_url=anvil_url,
        pk=ANVIL_DEFAULT_KEY,
        artifact_path=str(MEMORY_ANCHOR_ARTIFACT),
        constructor_args=[registry_addr],
    )

    # 3. Call the identity-bound path (F10 default).
    root_hex = "0x" + secrets.token_hex(32)
    result = anchor_memory(
        rpc_url=anvil_url,
        pk=ANVIL_DEFAULT_KEY,
        anchor_address=anchor_addr,
        root_hex=root_hex,
        identity_id=ALICE_IDENTITY_ID,
    )

    assert result["status"] == 1, (
        f"anchor tx should succeed, got status={result['status']}"
    )
    assert result["path"] == "identity"
    assert result["identity_id"] == ALICE_IDENTITY_ID
    assert result["event_emitted"] is True, "MemoryAnchored event must be emitted"
    # B5 / N9 — the event's topic[2] MUST carry the real identityId, not zero.
    assert result["event_identity_id_matches"] is True, (
        "MemoryAnchored event identityId topic did not match the expected id"
    )
    assert result["root"].lower() == root_hex.lower()


def test_anchor_memory_identity_bound_path_reverts_for_non_owner(anvil_url):
    """Caller must own the identity NFT — F10's whole point. Anchoring for an
    identity owned by a DIFFERENT address must revert with NotIdentityOwner."""
    deployer = cast_address_from_pk(ANVIL_DEFAULT_KEY)
    # A different EOA — anvil's account #1.
    other_pk = (
        "0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d"
    )

    # Mint identityId=99 to the OTHER account, then try to anchor as deployer.
    registry_addr = _deploy_mock_erc721_and_mint(
        anvil_url, ANVIL_DEFAULT_KEY, cast_address_from_pk(other_pk), 99
    )

    anchor_addr, _ = deploy_contract_via_cast(
        rpc_url=anvil_url,
        pk=ANVIL_DEFAULT_KEY,
        artifact_path=str(MEMORY_ANCHOR_ARTIFACT),
        constructor_args=[registry_addr],
    )

    root_hex = "0x" + secrets.token_hex(32)
    # Deployer doesn't own identity #99 — must fail. cast_send will either
    # raise (pre-flight failure) or return a tx whose receipt has status=0.
    try:
        result = anchor_memory(
            rpc_url=anvil_url,
            pk=ANVIL_DEFAULT_KEY,  # WRONG signer for identity 99
            anchor_address=anchor_addr,
            root_hex=root_hex,
            identity_id=99,
        )
    except RuntimeError:
        # cast_send raised because the pre-flight saw the revert — accept.
        return
    # If cast_send did NOT raise (e.g. some forks let the tx in but the
    # receipt comes back failed), the receipt status must be 0.
    assert result["status"] == 0, (
        f"expected revert (status=0) for non-owner anchor; got status={result['status']}"
    )


def test_anchor_memory_legacy_path_still_works(anvil_url):
    """Backward-compat: ``--legacy`` calls ``anchor(bytes32)``. Event emits
    with identityId=0. Not the production path, but kept reachable for
    smoke tests that don't have an ERC-8004 registry handy."""
    # Use a syntactically-valid but unrelated registry address; the legacy
    # path bypasses ownerOf entirely, so the registry contents don't matter.
    arc_identity_registry = "0x8004A818BFB912233c491871b3d84c89A494BD9e"
    anchor_addr, _ = deploy_contract_via_cast(
        rpc_url=anvil_url,
        pk=ANVIL_DEFAULT_KEY,
        artifact_path=str(MEMORY_ANCHOR_ARTIFACT),
        constructor_args=[arc_identity_registry],
    )

    root_hex = "0x" + secrets.token_hex(32)
    result = anchor_memory(
        rpc_url=anvil_url,
        pk=ANVIL_DEFAULT_KEY,
        anchor_address=anchor_addr,
        root_hex=root_hex,
        legacy=True,
    )

    assert result["status"] == 1
    assert result["path"] == "legacy"
    assert result["identity_id"] == 0
    assert result["event_emitted"] is True
    assert result["root"].lower() == root_hex.lower()


def test_anchor_memory_default_path_requires_identity_id():
    """Calling without identity_id and without legacy=True must raise — the
    function refuses to silently fall through to the unprotected path."""
    with pytest.raises(ValueError, match="identity_id is required"):
        anchor_memory(
            rpc_url="http://127.0.0.1:9999",  # unreachable; we won't reach RPC
            pk=ANVIL_DEFAULT_KEY,
            anchor_address="0x" + "00" * 20,
            root_hex="0x" + "11" * 32,
            # identity_id intentionally omitted, legacy intentionally False
        )


def test_anchor_memory_rejects_zero_identity_id():
    """identity_id=0 is the legacy sentinel — must be rejected on the
    identity path (callers can use legacy=True if they really want that)."""
    with pytest.raises(ValueError, match="must be a positive"):
        anchor_memory(
            rpc_url="http://127.0.0.1:9999",
            pk=ANVIL_DEFAULT_KEY,
            anchor_address="0x" + "00" * 20,
            root_hex="0x" + "11" * 32,
            identity_id=0,
        )
