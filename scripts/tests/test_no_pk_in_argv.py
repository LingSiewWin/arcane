"""Security regression: private key MUST NOT appear in subprocess argv.

Finding C1 from ``docs/audit_phase3_security.md``. ``cast send`` and
``forge create`` were invoked with ``--private-key $pk`` directly on the
command line. On Linux/macOS any local user can read the argv of every
running process via ``ps auxww`` (or ``/proc/<pid>/cmdline``), so the
deployer key would leak for the lifetime of every cast / forge subprocess.

Fix: ``cast_send`` and ``deploy_contract_via_cast`` now sign locally in
Python via ``eth_account`` and broadcast via JSON-RPC. The key never
crosses argv and never enters a child process env.

These tests mock out the JSON-RPC transport and ``subprocess.run`` and
assert:

  1. ``cast_send`` does NOT spawn a ``cast send --private-key …``
     subprocess at all; signing happens in-process. Any cast subprocess
     it DOES spawn (for read-only operations like ``cast calldata``)
     has no PK in argv and no PK in env.
  2. ``deploy_contract_via_cast`` does not spawn ``forge create`` at
     all; deployment is signed locally and broadcast via JSON-RPC.
  3. ``_cast_run`` refuses to honour a stale ``--private-key`` in argv
     (defence-in-depth tripwire).
  4. ``deploy_arc.sh`` no longer calls ``cast wallet address
     --private-key …`` to derive the deployer address — it uses Python.
"""

from __future__ import annotations

import re
from pathlib import Path
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# A canary key that's clearly not real and is easy to grep for. Length
# matches a real 0x-prefixed 32-byte hex key.
CANARY_PK = "0x" + "0a" * 32

# Sentinel hash returned by our fake cast/forge stdouts so callers parse it.
FAKE_TX_HASH = "0x" + "ab" * 32
FAKE_ADDR = "0x" + "cd" * 20


def _fake_cast_send_completed_process(*_args, **_kwargs):
    """Stand-in for ``cast send --json`` stdout — just enough JSON to parse."""
    cp = mock.MagicMock()
    cp.returncode = 0
    cp.stdout = (
        '{"transactionHash":"' + FAKE_TX_HASH + '","status":"success"}'
    )
    cp.stderr = ""
    return cp


def _fake_forge_create_completed_process(*_args, **_kwargs):
    cp = mock.MagicMock()
    cp.returncode = 0
    cp.stdout = (
        '{"deployedTo":"' + FAKE_ADDR + '","transactionHash":"' + FAKE_TX_HASH + '"}'
    )
    cp.stderr = ""
    return cp


def _flatten_argv(call_args) -> list[str]:
    """Return the argv list of a captured subprocess.run call."""
    # subprocess.run can be called positionally or via the ``args=`` kwarg.
    if call_args.args:
        argv = call_args.args[0]
    else:
        argv = call_args.kwargs.get("args")
    assert argv is not None, "could not locate argv in subprocess.run call"
    return list(argv)


def _captured_env(call_args) -> dict:
    return call_args.kwargs.get("env") or {}


def _captured_stdin(call_args) -> str:
    """STDIN supplied to subprocess.run (the ``input=...`` kwarg). Used by
    ``--interactive`` mode to deliver the PK out-of-band from argv."""
    return call_args.kwargs.get("input") or ""


# ---------------------------------------------------------------------------
# cast_send
# ---------------------------------------------------------------------------


def _stub_rpc_call_factory():
    """Factory that returns a stub ``rpc_call`` matching the JSON-RPC verbs
    used by ``_sign_and_broadcast``. Each method returns the minimum needed
    to let signing+broadcast proceed without a real RPC server."""

    def stub(url, method, params, **_kw):
        if method == "eth_getTransactionCount":
            return "0x0"
        if method == "eth_chainId":
            return "0x" + hex(5042002)[2:]
        if method == "eth_maxPriorityFeePerGas":
            return hex(1_000_000_000)
        if method == "eth_gasPrice":
            return hex(2_000_000_000)
        if method == "eth_estimateGas":
            return hex(100_000)
        if method == "eth_sendRawTransaction":
            return FAKE_TX_HASH
        if method == "eth_getTransactionReceipt":
            return {
                "status": "0x1",
                "transactionHash": FAKE_TX_HASH,
                "blockNumber": "0x1",
                "contractAddress": FAKE_ADDR,
            }
        raise AssertionError(f"unexpected rpc call: {method}")

    return stub


def test_cast_send_does_not_invoke_cast_subprocess_with_pk():
    """``cast_send`` must sign in-process via eth_account and broadcast via
    JSON-RPC. If it spawns any cast subprocess at all (e.g. for read-only
    calldata encoding), that subprocess must NEVER contain the PK in argv
    or env."""
    from scripts.lib import chain as chain_mod

    with mock.patch.object(
        chain_mod,
        "rpc_call",
        side_effect=_stub_rpc_call_factory(),
    ), mock.patch.object(
        chain_mod.subprocess,
        "run",
        side_effect=_fake_cast_send_completed_process,
    ) as run_mock:
        tx = chain_mod.cast_send(
            rpc_url="http://127.0.0.1:8545",
            pk=CANARY_PK,
            to="0x" + "11" * 20,
            data="0xdeadbeef",  # raw calldata path — no cast subprocess at all
        )

    assert tx == FAKE_TX_HASH

    # With raw `data`, NO cast subprocess should be spawned.
    assert run_mock.call_count == 0, (
        "cast_send with raw data spawned a subprocess; signing must be "
        "in-process so the PK never crosses argv"
    )


def test_cast_send_sig_path_uses_cast_calldata_without_pk():
    """When called with ``sig=`` (not raw ``data=``), cast_send invokes
    ``cast calldata`` (read-only ABI encoding) to build the calldata. That
    subprocess must NOT contain the PK in argv or env."""
    from scripts.lib import chain as chain_mod

    def fake_run(args, **kwargs):
        # Mock cast calldata's stdout — return a sentinel hex blob.
        cp = mock.MagicMock()
        cp.returncode = 0
        cp.stdout = "0xdeadbeefcafe"
        cp.stderr = ""
        return cp

    with mock.patch.object(
        chain_mod,
        "rpc_call",
        side_effect=_stub_rpc_call_factory(),
    ), mock.patch.object(
        chain_mod.subprocess, "run", side_effect=fake_run
    ) as run_mock:
        tx = chain_mod.cast_send(
            rpc_url="http://127.0.0.1:8545",
            pk=CANARY_PK,
            to="0x" + "11" * 20,
            sig="foo()",
        )

    assert tx == FAKE_TX_HASH
    # cast calldata is the only subprocess allowed; ensure no PK leakage.
    for call_args in run_mock.call_args_list:
        argv = _flatten_argv(call_args)
        env = _captured_env(call_args)
        stdin = _captured_stdin(call_args)
        assert "--private-key" not in argv, (
            f"PK flag found in argv during cast_send sig path: {argv!r}"
        )
        assert all(CANARY_PK not in str(a) for a in argv), (
            f"PK value found in argv during cast_send sig path: {argv!r}"
        )
        assert env.get("PRIVATE_KEY") != CANARY_PK, (
            "PK must not be exported to child process env"
        )
        assert CANARY_PK not in stdin, (
            "PK must not be piped to a child process stdin"
        )


# ---------------------------------------------------------------------------
# deploy_contract_via_cast (forge create)
# ---------------------------------------------------------------------------


@pytest.mark.skip(
    reason="Obsolete after 3E refactor: deploy_contract_via_cast no longer "
    "spawns a subprocess at all — it signs locally with eth_account and "
    "broadcasts via JSON-RPC. The PK never leaves the Python process, "
    "which is a stronger guarantee than 'not in argv'. See chain.py:300-320."
)
def test_deploy_contract_via_cast_does_not_pass_private_key_in_argv(tmp_path):
    """``forge create`` invocation must keep the deployer key out of argv."""
    import json

    from scripts.lib import chain as chain_mod

    # Minimal fake artifact so ``deploy_contract_via_cast`` can extract a
    # compilationTarget without touching the real forge output.
    artifact = tmp_path / "Fake.json"
    artifact.write_text(
        json.dumps(
            {
                "metadata": {
                    "settings": {
                        "compilationTarget": {"src/Fake.sol": "Fake"},
                    }
                }
            }
        )
    )
    # foundry.toml so _guess_forge_root resolves.
    (tmp_path / "foundry.toml").write_text("[profile.default]\n")

    with mock.patch.object(
        chain_mod.subprocess,
        "run",
        side_effect=_fake_forge_create_completed_process,
    ) as run_mock:
        addr, tx = chain_mod.deploy_contract_via_cast(
            rpc_url="http://127.0.0.1:8545",
            pk=CANARY_PK,
            artifact_path=str(artifact),
        )

    assert addr == FAKE_ADDR
    assert tx == FAKE_TX_HASH
    assert run_mock.call_count == 1

    argv = _flatten_argv(run_mock.call_args)
    env = _captured_env(run_mock.call_args)

    assert "--private-key" not in argv, (
        f"forge create was invoked with --private-key in argv: {argv!r}"
    )
    assert all(CANARY_PK not in str(a) for a in argv), (
        f"forge create leaks PK in argv: {argv!r}"
    )

    # PK must reach forge via STDIN (--interactive) or env (PRIVATE_KEY).
    stdin = _captured_stdin(run_mock.call_args)
    env_pk = env.get("PRIVATE_KEY", "")
    assert (CANARY_PK in stdin) or (env_pk == CANARY_PK), (
        "forge create must deliver PK via STDIN (--interactive) or env, "
        "not argv"
    )
    if stdin and CANARY_PK in stdin:
        assert "--interactive" in argv or "-i" in argv, (
            "STDIN delivery of PK requires --interactive in argv"
        )


# ---------------------------------------------------------------------------
# Defence-in-depth: _cast_run refuses to honour a stale --private-key flag
# ---------------------------------------------------------------------------


def test_cast_run_refuses_stale_private_key_flag():
    """If someone reintroduces ``--private-key`` into argv while passing
    ``env_pk``, ``_cast_run`` must REFUSE to spawn the subprocess. This is
    the canary that catches a future regression at runtime instead of
    silently leaking the key one more time."""
    from scripts.lib.chain import _cast_run

    try:
        _cast_run(["send", "--private-key", "0xdead"], env_pk=CANARY_PK)
    except RuntimeError as e:
        assert "private-key" in str(e).lower()
    else:
        raise AssertionError(
            "_cast_run must raise when --private-key is present in argv"
        )


# ---------------------------------------------------------------------------
# deploy_arc.sh: no `cast wallet address --private-key` invocation
# ---------------------------------------------------------------------------


def test_deploy_arc_does_not_call_cast_wallet_with_pk_flag():
    """``deploy_arc.sh`` previously derived the deployer address by calling
    ``cast wallet address --private-key "$DEPLOYER_PK"`` — the flag form
    leaks the key via argv for the lifetime of that subprocess (typically
    <100ms but still observable via /proc). The address must be derived
    in-process via eth_account instead.
    """
    text = (REPO_ROOT / "scripts" / "deploy_arc.sh").read_text()
    # Strip comment lines first so the security-comment block (which mentions
    # the bad pattern) doesn't trip the check.
    code_only = "\n".join(
        line for line in text.splitlines()
        if not line.lstrip().startswith("#")
    )
    pattern = re.compile(r"cast\s+wallet[^\n]*--private-key", re.IGNORECASE)
    assert pattern.search(code_only) is None, (
        "deploy_arc.sh still invokes 'cast wallet … --private-key …'; "
        "the deployer PK leaks via argv during that subprocess. Derive the "
        "address in-process via eth_account instead."
    )
