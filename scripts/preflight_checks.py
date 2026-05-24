"""preflight_checks — individual pre-broadcast checks for AgoraHack.

Each check is a small pure function that returns a ``CheckResult``. The
``preflight`` CLI composes these and prints a summary; tests in
``scripts/tests/test_preflight.py`` exercise positive + negative cases for
each one.

Design notes:

* No check ever prints the deployer private key. Checks that need the PK take
  it as an argument and only return the derived address (or a redacted
  prefix) in their evidence.
* Checks return three levels: GREEN (pass), YELLOW (warn but proceed),
  RED (cannot proceed to live mode). YELLOW exists so e.g. an existing
  ``/tmp/darkpool_nonces.db`` from a prior demo can be flagged without
  blocking; RED is reserved for "broadcast will definitely fail".
* RPC checks use a 5s timeout — Arc testnet is usually responsive; longer
  than 5s suggests a network or token issue.
* The USDC balance check makes a real ``eth_call`` to the USDC contract at
  ``0x3600…`` and does NOT mock the result. If the user has 0 USDC, that's
  what we report.

Public API (consumed by ``scripts/preflight.py``):

    from scripts.preflight_checks import (
        CheckResult, Severity,
        check_rpc, check_deployer_pk_present, check_deployer_address,
        check_usdc_balance, check_anvil_available, check_forge_available,
        check_alice_memory_seeded, check_python_venv, check_node_modules,
        check_no_old_nonce_db, check_contracts_compiled,
        check_demo_output_writable, check_env_loaded,
    )
"""

from __future__ import annotations

import enum
import os
import re
import shutil
import sqlite3
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import httpx


# Arc testnet canonical constants — pulled from
# ~/.arc-canteen/context/docs/circlefin-skills/use-arc.md
ARC_TESTNET_CHAIN_ID = 5042002
USDC_ADDR = "0x3600000000000000000000000000000000000000"
USDC_DECIMALS = 6
FAUCET_URL = "https://faucet.circle.com"
EXPLORER_URL = "https://testnet.arcscan.app"


REPO_ROOT = Path(__file__).resolve().parent.parent


class Severity(enum.Enum):
    GREEN = "GREEN"
    YELLOW = "YELLOW"
    RED = "RED"


@dataclass
class CheckResult:
    """Result of one check.

    Attributes:
        name: short stable identifier, e.g. "rpc".
        severity: GREEN / YELLOW / RED.
        message: human-readable one-liner, displayed by the CLI.
        next_step: actionable hint for RED/YELLOW cases (URL, command).
        evidence: structured data for tests + machine-readable output.
    """

    name: str
    severity: Severity
    message: str
    next_step: Optional[str] = None
    evidence: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        """True for GREEN, False otherwise. YELLOW does not block live mode
        by itself but counts as 'not ok' for the SUMMARY tally."""
        return self.severity is Severity.GREEN

    @property
    def is_red(self) -> bool:
        return self.severity is Severity.RED


# ---------------------------------------------------------------------------
# Env / config
# ---------------------------------------------------------------------------


def _read_canteen_env() -> dict[str, str]:
    """Source ~/.arc-canteen/env via bash and return its exported vars.

    Returns an empty dict if the file doesn't exist or fails to source. We
    deliberately do NOT export these into the current process — callers can
    decide whether to overlay them onto os.environ.
    """
    env_file = Path.home() / ".arc-canteen" / "env"
    if not env_file.exists():
        return {}
    try:
        out = subprocess.run(
            ["bash", "-c", f". {env_file} && env"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return {}
    if out.returncode != 0:
        return {}
    parsed: dict[str, str] = {}
    for line in out.stdout.splitlines():
        if "=" not in line:
            continue
        k, _, v = line.partition("=")
        parsed[k] = v
    return parsed


def check_env_loaded() -> CheckResult:
    """Check that we have a usable RPC URL — either in process env or via
    ~/.arc-canteen/env. Returns the (masked) source.

    GREEN if $RPC is present in either source.
    RED otherwise — we cannot run any other live-mode check.
    """
    rpc = os.environ.get("RPC", "").strip()
    source = "os.environ"
    if not rpc:
        canteen = _read_canteen_env()
        rpc = canteen.get("RPC", "").strip()
        source = "~/.arc-canteen/env" if rpc else "none"

    if not rpc:
        return CheckResult(
            name="env_loaded",
            severity=Severity.RED,
            message="No RPC URL found in $RPC or ~/.arc-canteen/env",
            next_step=(
                "Set $RPC to your Arc testnet RPC URL.\n"
                "    export RPC=https://rpc.testnet.arc.network"
            ),
            evidence={"source": "none"},
        )
    return CheckResult(
        name="env_loaded",
        severity=Severity.GREEN,
        message=f"RPC URL available (source: {source})",
        evidence={"source": source, "rpc_masked": _mask_rpc(rpc)},
    )


def _mask_rpc(url: str) -> str:
    """Replace token-like path segments with <redacted>."""
    if not url:
        return "<unset>"
    return re.sub(r"(swrm_)[A-Za-z0-9]+", r"\1<redacted>", url)


def resolve_rpc(explicit: Optional[str] = None) -> str:
    """Resolve the effective RPC URL: explicit arg > $RPC > canteen env > ''."""
    if explicit:
        return explicit
    rpc = os.environ.get("RPC", "").strip()
    if rpc:
        return rpc
    return _read_canteen_env().get("RPC", "").strip()


# ---------------------------------------------------------------------------
# RPC reachability
# ---------------------------------------------------------------------------


def check_rpc(
    url: str,
    *,
    expected_chain_id: int = ARC_TESTNET_CHAIN_ID,
    timeout: float = 5.0,
) -> CheckResult:
    """eth_chainId returns expected_chain_id, eth_blockNumber > 0.

    Hits the real RPC. Times out fast (5s) so a dead URL doesn't hang the
    whole preflight run. We don't mock this in tests where $RPC is set; we
    actually hit Arc.
    """
    if not url:
        return CheckResult(
            name="rpc",
            severity=Severity.RED,
            message="RPC URL is empty",
            next_step=f"Set $RPC to your Arc testnet RPC URL (see {FAUCET_URL})",
            evidence={},
        )
    try:
        with httpx.Client(timeout=timeout) as client:
            chain_resp = client.post(
                url,
                json={"jsonrpc": "2.0", "id": 1, "method": "eth_chainId", "params": []},
            )
            chain_resp.raise_for_status()
            chain_data = chain_resp.json()
            if "error" in chain_data and chain_data["error"]:
                return CheckResult(
                    name="rpc",
                    severity=Severity.RED,
                    message=f"RPC eth_chainId error: {chain_data['error']}",
                    next_step="Verify $RPC URL is a valid Arc testnet endpoint.",
                    evidence={"rpc_masked": _mask_rpc(url)},
                )
            chain_id = int(chain_data["result"], 16)

            block_resp = client.post(
                url,
                json={
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "eth_blockNumber",
                    "params": [],
                },
            )
            block_resp.raise_for_status()
            block_data = block_resp.json()
            block_number = int(block_data["result"], 16)
    except httpx.TimeoutException:
        return CheckResult(
            name="rpc",
            severity=Severity.RED,
            message=f"RPC timed out after {timeout}s",
            next_step=(
                f"Check network connectivity and that $RPC points at a "
                f"live Arc testnet endpoint ({_mask_rpc(url)})"
            ),
            evidence={"rpc_masked": _mask_rpc(url)},
        )
    except (httpx.HTTPError, ValueError, KeyError) as e:
        return CheckResult(
            name="rpc",
            severity=Severity.RED,
            message=f"RPC unreachable or returned malformed JSON: {type(e).__name__}: {str(e)[:120]}",
            next_step=f"Re-check $RPC ({_mask_rpc(url)}) and try again.",
            evidence={"rpc_masked": _mask_rpc(url)},
        )

    if chain_id != expected_chain_id:
        return CheckResult(
            name="rpc",
            severity=Severity.RED,
            message=(
                f"Chain id mismatch: got {chain_id}, expected "
                f"{expected_chain_id} (Arc Testnet). Pointing at a different "
                f"chain will spend USDC against the wrong contracts."
            ),
            next_step=(
                f"Set $RPC to https://rpc.testnet.arc.network "
                f"(or a token-gated equivalent)."
            ),
            evidence={
                "chain_id": chain_id,
                "expected_chain_id": expected_chain_id,
                "block_number": block_number,
                "rpc_masked": _mask_rpc(url),
            },
        )

    if block_number <= 0:
        return CheckResult(
            name="rpc",
            severity=Severity.YELLOW,
            message=f"RPC reachable but block_number={block_number} (chain may be stalled)",
            next_step="Wait a few seconds and re-run preflight.",
            evidence={
                "chain_id": chain_id,
                "block_number": block_number,
                "rpc_masked": _mask_rpc(url),
            },
        )

    return CheckResult(
        name="rpc",
        severity=Severity.GREEN,
        message=(
            f"Arc Testnet RPC reachable (chain {chain_id}, "
            f"block {block_number:,})"
        ),
        evidence={
            "chain_id": chain_id,
            "block_number": block_number,
            "rpc_masked": _mask_rpc(url),
        },
    )


# ---------------------------------------------------------------------------
# Deployer key
# ---------------------------------------------------------------------------


_HEX_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")


def check_deployer_key() -> CheckResult:
    """Deployer key is resolvable for live broadcast.

    GREEN if EITHER:
      * an encrypted Foundry keystore account is resolvable non-interactively
        (``--account``/``DEPLOYER_ACCOUNT`` names a keystore that exists AND
        ``KEYSTORE_PASSWORD`` is set), OR
      * ``DEPLOYER_PK`` is set + well-formed.

    The keystore path is preferred (Circle's use-arc guidance). When a
    keystore account is named but its password isn't available
    non-interactively, we return YELLOW: live broadcast will still work via
    an interactive prompt, but preflight can't prove it green-light without a
    human. RED only when no key source exists at all.

    NEVER prints the key or password. Evidence carries only a redacted
    prefix / the account name.
    """
    account = os.environ.get("DEPLOYER_ACCOUNT", "").strip()

    # --- Preferred: encrypted keystore account ----------------------------
    if account:
        from scripts.lib.keys import keystore_path

        path = keystore_path(account)
        if not path.exists():
            return CheckResult(
                name="deployer_key",
                severity=Severity.RED,
                message=(
                    f"DEPLOYER_ACCOUNT='{account}' but no keystore at {path}"
                ),
                next_step=(
                    f"Create the keystore:\n"
                    f"    cast wallet import {account} --interactive"
                ),
                evidence={"account": account, "keystore_exists": False},
            )
        if os.environ.get("KEYSTORE_PASSWORD") is not None:
            return CheckResult(
                name="deployer_key",
                severity=Severity.GREEN,
                message=(
                    f"Deployer key via encrypted keystore '{account}' "
                    f"(password from KEYSTORE_PASSWORD)"
                ),
                evidence={"account": account, "keystore_exists": True, "source": "keystore"},
            )
        return CheckResult(
            name="deployer_key",
            severity=Severity.YELLOW,
            message=(
                f"Keystore '{account}' found; password will be requested "
                f"interactively at broadcast time"
            ),
            next_step=(
                "Either run live broadcast in an interactive terminal (you'll "
                "be prompted for the password), or set KEYSTORE_PASSWORD to "
                "go fully non-interactive."
            ),
            evidence={"account": account, "keystore_exists": True, "source": "keystore"},
        )

    # --- Fallback: DEPLOYER_PK env ----------------------------------------
    pk = os.environ.get("DEPLOYER_PK", "")
    if not pk:
        return CheckResult(
            name="deployer_key",
            severity=Severity.RED,
            message="No deployer key: neither DEPLOYER_ACCOUNT (keystore) nor DEPLOYER_PK set",
            next_step=(
                "Preferred — encrypted keystore (key never in an env var):\n"
                "    cast wallet import deployer --interactive\n"
                "    export DEPLOYER_ACCOUNT=deployer   # + KEYSTORE_PASSWORD or interactive\n"
                "Fallback — plain-text key:\n"
                "    export DEPLOYER_PK=0x<64-hex-chars>"
            ),
            evidence={},
        )
    if not pk.startswith("0x") or not _HEX_RE.match(pk):
        return CheckResult(
            name="deployer_key",
            severity=Severity.RED,
            message=(
                f"DEPLOYER_PK is not a valid 32-byte 0x-prefixed hex key "
                f"(length={len(pk)}, expected 66 incl. 0x)"
            ),
            next_step=(
                "Verify the key is 0x + 64 hex chars. Regenerate with "
                "`cast wallet new` if unsure, or prefer a keystore "
                "(`cast wallet import deployer --interactive`)."
            ),
            evidence={"length": len(pk)},
        )
    return CheckResult(
        name="deployer_key",
        severity=Severity.GREEN,
        message="Deployer key via DEPLOYER_PK (32 bytes, 0x-prefixed)",
        evidence={"prefix": pk[:6] + "…", "source": "env_pk"},
    )


def check_deployer_pk_present() -> CheckResult:
    """$DEPLOYER_PK is set, starts with 0x, is exactly 64 hex chars (32 bytes).

    Doesn't print the key. Evidence carries only a 6-char prefix for
    correlation between runs.

    Retained for backward-compat (existing tests + the plain-text-key path).
    New code should prefer ``check_deployer_key()``, which also accepts an
    encrypted keystore account.
    """
    pk = os.environ.get("DEPLOYER_PK", "")
    if not pk:
        return CheckResult(
            name="deployer_pk",
            severity=Severity.RED,
            message="DEPLOYER_PK is not set",
            next_step=(
                "Generate or import a deployer key:\n"
                "    cast wallet new                # generate fresh\n"
                "    export DEPLOYER_PK=0x<64-hex-chars>"
            ),
            evidence={},
        )
    if not pk.startswith("0x"):
        return CheckResult(
            name="deployer_pk",
            severity=Severity.RED,
            message="DEPLOYER_PK must start with 0x",
            next_step='Re-export with the 0x prefix: export DEPLOYER_PK=0x...',
            evidence={"length": len(pk)},
        )
    if not _HEX_RE.match(pk):
        return CheckResult(
            name="deployer_pk",
            severity=Severity.RED,
            message=(
                f"DEPLOYER_PK is not a valid 32-byte hex key "
                f"(length={len(pk)}, expected 66 incl. 0x)"
            ),
            next_step=(
                "Verify the key is 64 lowercase hex chars after 0x. "
                "Regenerate with `cast wallet new` if unsure."
            ),
            evidence={"length": len(pk)},
        )
    return CheckResult(
        name="deployer_pk",
        severity=Severity.GREEN,
        message="DEPLOYER_PK is set (32 bytes, 0x-prefixed)",
        evidence={"prefix": pk[:6] + "…"},
    )


def check_deployer_address(pk: Optional[str] = None) -> CheckResult:
    """Derive the deployer EOA address from the resolved deployer key.

    Resolves the key the same way live mode does: explicit ``pk`` arg, else
    a keystore account (``DEPLOYER_ACCOUNT`` + ``KEYSTORE_PASSWORD``, decrypted
    non-interactively), else ``DEPLOYER_PK``. NEVER prompts here — preflight
    is non-interactive — and NEVER prints the key.

    GREEN if we can derive an address.
    RED if no key source is resolvable or derivation failed.
    YELLOW if a keystore account is named but its password isn't available
    non-interactively (address can't be derived without a human, but live
    broadcast can still prompt).
    """
    if pk is None:
        # No explicit key — resolve from env the same way live mode will.
        from scripts.lib.keys import KeyResolutionError, resolve_deployer_key

        account = os.environ.get("DEPLOYER_ACCOUNT", "").strip()
        if account and os.environ.get("KEYSTORE_PASSWORD") is None:
            # Keystore named but no non-interactive password — can't derive
            # the address without prompting, which preflight must not do.
            return CheckResult(
                name="deployer_address",
                severity=Severity.YELLOW,
                message=(
                    f"Deployer address not derivable non-interactively for "
                    f"keystore '{account}' (no KEYSTORE_PASSWORD)"
                ),
                next_step=(
                    "Set KEYSTORE_PASSWORD to verify the address + USDC "
                    "balance in preflight, or proceed and enter the password "
                    "at broadcast time."
                ),
                evidence={"account": account},
            )
        if account or os.environ.get("DEPLOYER_PK", "").strip():
            try:
                pk = resolve_deployer_key(account=account or None, allow_interactive=False)
            except KeyResolutionError as e:
                return CheckResult(
                    name="deployer_address",
                    severity=Severity.RED,
                    message=f"Cannot resolve deployer key: {type(e).__name__}",
                    next_step="See the deployer_key check for both options.",
                    evidence={},
                )
        else:
            pk = ""
    if not pk:
        return CheckResult(
            name="deployer_address",
            severity=Severity.RED,
            message="Cannot derive address: no deployer key set",
            next_step="Set DEPLOYER_ACCOUNT (+ KEYSTORE_PASSWORD) or DEPLOYER_PK (see deployer_key check)",
            evidence={},
        )
    try:
        from eth_account import Account

        addr = Account.from_key(pk).address
    except Exception as e:  # noqa: BLE001 - eth_account raises many types
        return CheckResult(
            name="deployer_address",
            severity=Severity.RED,
            message=f"Failed to derive address from DEPLOYER_PK: {type(e).__name__}",
            next_step="Verify DEPLOYER_PK is a valid 32-byte hex private key.",
            evidence={},
        )
    return CheckResult(
        name="deployer_address",
        severity=Severity.GREEN,
        message=f"Deployer EOA: {addr}",
        evidence={"address": addr},
    )


# ---------------------------------------------------------------------------
# USDC balance — pays for gas + bond on Arc
# ---------------------------------------------------------------------------


def _usdc_balance_call(rpc_url: str, addr: str, *, timeout: float = 5.0) -> int:
    """Raw ``USDC.balanceOf(addr) -> uint256``. Returns the raw integer in
    base units (6 decimals). Real eth_call, no mocking."""
    # balanceOf(address) selector = 0x70a08231
    # padded address is 12 zero bytes + 20-byte address
    addr_clean = addr.removeprefix("0x").lower().rjust(40, "0")
    data = "0x70a08231" + "0" * 24 + addr_clean
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "eth_call",
        "params": [{"to": USDC_ADDR, "data": data}, "latest"],
    }
    with httpx.Client(timeout=timeout) as client:
        resp = client.post(rpc_url, json=body)
        resp.raise_for_status()
        data_json = resp.json()
    if "error" in data_json and data_json["error"]:
        raise RuntimeError(f"USDC balanceOf RPC error: {data_json['error']}")
    result = data_json.get("result", "0x0") or "0x0"
    return int(result, 16)


def check_usdc_balance(
    addr: str,
    rpc_url: str,
    *,
    min_usdc: float = 2.0,
    timeout: float = 5.0,
) -> CheckResult:
    """USDC.balanceOf(addr) >= min_usdc (in human USDC, 6-decimal token).

    Hits the real USDC contract on Arc — does NOT mock.
    """
    if not addr or not rpc_url:
        return CheckResult(
            name="usdc_balance",
            severity=Severity.RED,
            message="Cannot check USDC balance: missing addr or rpc_url",
            next_step="Run deployer_address + rpc checks first.",
            evidence={},
        )
    try:
        raw = _usdc_balance_call(rpc_url, addr, timeout=timeout)
    except Exception as e:  # noqa: BLE001
        return CheckResult(
            name="usdc_balance",
            severity=Severity.RED,
            message=f"USDC.balanceOf failed: {type(e).__name__}: {str(e)[:120]}",
            next_step=(
                "Verify the RPC is reachable + USDC address is correct "
                f"({USDC_ADDR})."
            ),
            evidence={"address": addr},
        )
    human = raw / (10 ** USDC_DECIMALS)
    if human < min_usdc:
        return CheckResult(
            name="usdc_balance",
            severity=Severity.RED,
            message=f"USDC balance: {human:.6f} USDC — need {min_usdc:.2f} USDC minimum",
            next_step=(
                f"Fund {addr} from {FAUCET_URL} (request >= {min_usdc:.2f} USDC).\n"
                f"    Verify after a minute with:\n"
                f"    cast call --rpc-url $RPC {USDC_ADDR} 'balanceOf(address)' {addr}"
            ),
            evidence={
                "address": addr,
                "balance_raw": raw,
                "balance_human": human,
                "min_required": min_usdc,
            },
        )
    return CheckResult(
        name="usdc_balance",
        severity=Severity.GREEN,
        message=f"USDC balance: {human:.6f} USDC (>= {min_usdc:.2f} required)",
        evidence={
            "address": addr,
            "balance_raw": raw,
            "balance_human": human,
            "min_required": min_usdc,
        },
    )


# ---------------------------------------------------------------------------
# Foundry tools on PATH
# ---------------------------------------------------------------------------


def _tool_version(tool: str) -> CheckResult:
    binary = shutil.which(tool)
    if binary is None:
        return CheckResult(
            name=tool,
            severity=Severity.RED,
            message=f"{tool} not found on PATH",
            next_step=(
                "Install Foundry:\n"
                "    curl -L https://foundry.paradigm.xyz | bash && foundryup"
            ),
            evidence={},
        )
    try:
        out = subprocess.run(
            [tool, "--version"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        return CheckResult(
            name=tool,
            severity=Severity.RED,
            message=f"{tool} --version failed: {type(e).__name__}",
            next_step="Re-install Foundry (`foundryup`)",
            evidence={"path": binary},
        )
    if out.returncode != 0:
        return CheckResult(
            name=tool,
            severity=Severity.RED,
            message=f"{tool} --version exited {out.returncode}",
            next_step="Re-install Foundry (`foundryup`)",
            evidence={"path": binary, "stderr": out.stderr.strip()[:200]},
        )
    version_line = (out.stdout or out.stderr).splitlines()[0].strip() if (out.stdout or out.stderr) else ""
    return CheckResult(
        name=tool,
        severity=Severity.GREEN,
        message=f"{version_line or tool + ' available'}",
        evidence={"path": binary, "version_line": version_line},
    )


def check_anvil_available() -> CheckResult:
    return _tool_version("anvil")


def check_forge_available() -> CheckResult:
    return _tool_version("forge")


def check_cast_available() -> CheckResult:
    return _tool_version("cast")


# ---------------------------------------------------------------------------
# Alice memory file
# ---------------------------------------------------------------------------


def check_alice_memory_seeded(
    path: str = "/tmp/alice.mem",
    *,
    min_entries: int = 5000,
    min_pinned: int = 3,
) -> CheckResult:
    """Verify the seeded Alice memory exists and meets the demo's expectations.

    NOTE: A full validation of the pinned Merkle root would require running
    Alice's hash function over the same DEFAULT_PINNED_RULES that the demo
    encodes on-chain. That comparison happens at runtime in the orchestrator
    itself (see `agents.bob.constitution_hash`); this check verifies the file
    is loadable + has enough content.

    GREEN if file exists + loads + meets thresholds.
    YELLOW if loadable but undersized (demo will still run but recall will
    be lower than the audited 92%).
    RED if missing or unloadable.
    """
    p = Path(path)
    if not p.exists():
        return CheckResult(
            name="alice_memory",
            severity=Severity.RED,
            message=f"Alice memory not found at {path}",
            next_step=(
                "Seed it:\n"
                f"    agents/.venv/bin/python -m agents.seed_alice --out {path}"
            ),
            evidence={"path": path, "exists": False},
        )
    try:
        from agents.memory_service import MemoryService

        mem = MemoryService.load(path)
    except Exception as e:  # noqa: BLE001
        return CheckResult(
            name="alice_memory",
            severity=Severity.RED,
            message=f"Alice memory not loadable: {type(e).__name__}: {str(e)[:120]}",
            next_step=(
                f"Re-seed:\n"
                f"    agents/.venv/bin/python -m agents.seed_alice --out {path} --force"
            ),
            evidence={"path": path, "exists": True},
        )
    n_entries = len(mem)
    pinned = mem.pinned_ids()
    n_pinned = len(pinned)
    base_evidence = {
        "path": path,
        "entries": n_entries,
        "pinned": n_pinned,
        "min_entries": min_entries,
        "min_pinned": min_pinned,
    }

    if n_pinned < min_pinned:
        return CheckResult(
            name="alice_memory",
            severity=Severity.RED,
            message=(
                f"Alice memory missing pinned entries: {n_pinned}/{min_pinned}. "
                f"Demo step 5 (anchor pinned root) requires real pinned rules."
            ),
            next_step=(
                f"Re-seed:\n"
                f"    agents/.venv/bin/python -m agents.seed_alice --out {path} --force"
            ),
            evidence=base_evidence,
        )
    if n_entries < min_entries:
        return CheckResult(
            name="alice_memory",
            severity=Severity.YELLOW,
            message=(
                f"Alice memory has only {n_entries} entries (< {min_entries}). "
                f"Demo will run but recall is below the audited 92%."
            ),
            next_step=(
                f"For full recall, rebuild:\n"
                f"    agents/.venv/bin/python -m agents.seed_alice --out {path} --force"
            ),
            evidence=base_evidence,
        )
    return CheckResult(
        name="alice_memory",
        severity=Severity.GREEN,
        message=(
            f"Alice memory seeded ({n_entries:,} entries, "
            f"{n_pinned} pinned)"
        ),
        evidence=base_evidence,
    )


# ---------------------------------------------------------------------------
# Python venv + agent deps
# ---------------------------------------------------------------------------


def check_python_venv(
    venv_python: Optional[str] = None,
) -> CheckResult:
    """`agents/.venv/bin/python` exists + has the demo deps installed.

    Required imports: memory_service (Slice 1), dark_pool + x402_client (Slice 4),
    orchestrator + alice + bob + seed_alice (Slice 5A), nonce_store +
    rate_limiter (Slice 5B), eth_account, httpx, fastapi.
    """
    py = venv_python or str(REPO_ROOT / "agents" / ".venv" / "bin" / "python")
    if not Path(py).exists():
        return CheckResult(
            name="python_venv",
            severity=Severity.RED,
            message=f"Python venv not found at {py}",
            next_step=(
                "Create the venv:\n"
                "    python3 -m venv agents/.venv\n"
                "    agents/.venv/bin/pip install -r agents/requirements-darkpool.txt\n"
                "    agents/.venv/bin/pip install -r agents/requirements-memory.txt\n"
                "    agents/.venv/bin/pip install -r agents/requirements-orchestrator.txt"
            ),
            evidence={"python": py},
        )
    # Probe importability. Use a one-shot subprocess so a missing dep doesn't
    # crash this process and we still report the others.
    probe = (
        "import sys, importlib\n"
        "mods = [\n"
        "  'agents.memory_service',\n"
        "  'agents.dark_pool',\n"
        "  'agents.x402_client',\n"
        "  'agents.orchestrator',\n"
        "  'agents.alice',\n"
        "  'agents.bob',\n"
        "  'agents.seed_alice',\n"
        "  'agents.nonce_store',\n"
        "  'agents.rate_limiter',\n"
        "  'httpx', 'eth_account', 'fastapi',\n"
        "]\n"
        "missing = []\n"
        "for m in mods:\n"
        "    try:\n"
        "        importlib.import_module(m)\n"
        "    except Exception as e:\n"
        "        missing.append((m, type(e).__name__))\n"
        "if missing:\n"
        "    for m, et in missing: print(f'MISSING {m}: {et}')\n"
        "    sys.exit(1)\n"
        "print('OK')\n"
    )
    env = os.environ.copy()
    # ensure the venv can find the repo's agents/ package on path
    env["PYTHONPATH"] = str(REPO_ROOT) + ":" + env.get("PYTHONPATH", "")
    try:
        out = subprocess.run(
            [py, "-c", probe],
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
            check=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        return CheckResult(
            name="python_venv",
            severity=Severity.RED,
            message=f"Failed to probe venv: {type(e).__name__}",
            next_step="Verify agents/.venv/bin/python is executable.",
            evidence={"python": py},
        )
    if out.returncode != 0:
        return CheckResult(
            name="python_venv",
            severity=Severity.RED,
            message="Python venv missing dependencies",
            next_step=(
                "Install missing deps:\n"
                "    agents/.venv/bin/pip install -r agents/requirements-darkpool.txt\n"
                "    agents/.venv/bin/pip install -r agents/requirements-memory.txt\n"
                "    agents/.venv/bin/pip install -r agents/requirements-orchestrator.txt"
            ),
            evidence={
                "python": py,
                "missing": out.stdout.strip().splitlines(),
            },
        )
    return CheckResult(
        name="python_venv",
        severity=Severity.GREEN,
        message=f"Python venv ready ({py})",
        evidence={"python": py},
    )


# ---------------------------------------------------------------------------
# Node modules
# ---------------------------------------------------------------------------


def check_node_modules() -> CheckResult:
    """`node_modules/@circle-fin/x402-batching` exists.

    Slice 5C needs this. If absent, run `pnpm install` from repo root.
    """
    pkg = REPO_ROOT / "node_modules" / "@circle-fin" / "x402-batching" / "package.json"
    if not pkg.exists():
        return CheckResult(
            name="node_modules",
            severity=Severity.RED,
            message="@circle-fin/x402-batching not installed",
            next_step="Run `pnpm install` from repo root.",
            evidence={"package": str(pkg)},
        )
    return CheckResult(
        name="node_modules",
        severity=Severity.GREEN,
        message="@circle-fin/x402-batching installed",
        evidence={"package": str(pkg)},
    )


# ---------------------------------------------------------------------------
# Stale state — dark pool nonce DB
# ---------------------------------------------------------------------------


def check_no_old_nonce_db(
    db_path: str = "/tmp/darkpool_nonces.db",
    *,
    nontrivial_rows: int = 0,
) -> CheckResult:
    """Warn if a stale nonce DB exists with non-trivial content.

    Replay protection persists across runs. If the demo's deployer signed
    EIP-3009 authorizations in a prior run, the same nonces will be rejected
    in live mode. YELLOW unless the user has explicitly accepted; we never
    auto-delete a DB without permission.

    GREEN if db doesn't exist or is empty.
    YELLOW if db exists with content (instructs `rm`).
    RED if the DB is malformed (unreadable).
    """
    p = Path(db_path)
    if not p.exists():
        return CheckResult(
            name="nonce_db",
            severity=Severity.GREEN,
            message=f"No stale nonce DB at {db_path}",
            evidence={"path": db_path, "exists": False},
        )
    try:
        conn = sqlite3.connect(db_path)
        try:
            # The dark pool's table is named 'used_nonces' but we don't bind
            # to that here — just count nontrivial rows across any user tables.
            tables = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
            total_rows = 0
            for (tname,) in tables:
                row = conn.execute(
                    f"SELECT COUNT(*) FROM {tname}"
                ).fetchone()
                total_rows += int(row[0]) if row else 0
        finally:
            conn.close()
    except sqlite3.DatabaseError as e:
        return CheckResult(
            name="nonce_db",
            severity=Severity.RED,
            message=f"Existing nonce DB is corrupt: {type(e).__name__}",
            next_step=f"Remove it: rm {db_path}",
            evidence={"path": db_path, "exists": True},
        )
    if total_rows <= nontrivial_rows:
        return CheckResult(
            name="nonce_db",
            severity=Severity.GREEN,
            message=f"Nonce DB present but empty ({total_rows} rows)",
            evidence={"path": db_path, "rows": total_rows},
        )
    return CheckResult(
        name="nonce_db",
        severity=Severity.YELLOW,
        message=(
            f"Stale nonce DB at {db_path} has {total_rows} rows. "
            f"This is fine for local mode but may cause EIP-3009 replay "
            f"rejections in live mode with the same EOA."
        ),
        next_step=(
            f"If switching to live mode with a fresh EOA, remove it:\n"
            f"    rm {db_path}"
        ),
        evidence={"path": db_path, "rows": total_rows},
    )


# ---------------------------------------------------------------------------
# Contracts compiled
# ---------------------------------------------------------------------------


def check_contracts_compiled() -> CheckResult:
    """contracts/out/<Name>.sol/<Name>.json exists for each of the 4 deploys.

    GREEN if all 4 artifacts present.
    RED if any missing.
    """
    needed = [
        "ConstitutionRegistry",
        "ConstitutionHook",
        "MemoryAnchor",
        "BondVault",
    ]
    missing = []
    for name in needed:
        artifact = REPO_ROOT / "contracts" / "out" / f"{name}.sol" / f"{name}.json"
        if not artifact.exists():
            missing.append(str(artifact))
    if missing:
        return CheckResult(
            name="contracts_compiled",
            severity=Severity.RED,
            message=(
                f"{len(missing)}/{len(needed)} contract artifacts missing "
                f"— deploy_arc.sh will fail"
            ),
            next_step="cd contracts && forge build && cd ..",
            evidence={"missing": missing},
        )
    return CheckResult(
        name="contracts_compiled",
        severity=Severity.GREEN,
        message=f"All {len(needed)} contract artifacts present",
        evidence={"contracts": needed},
    )


def check_contracts_buildable() -> CheckResult:
    """Convenience alias matching the spec's named function. Runs
    `forge build --offline` against ``contracts/`` and reports.

    Slower than ``check_contracts_compiled`` because it shells out to forge.
    Tests should mock or skip this if forge is unavailable.
    """
    if shutil.which("forge") is None:
        return CheckResult(
            name="contracts_buildable",
            severity=Severity.RED,
            message="forge not on PATH",
            next_step="Install Foundry.",
            evidence={},
        )
    contracts_dir = REPO_ROOT / "contracts"
    if not contracts_dir.exists():
        return CheckResult(
            name="contracts_buildable",
            severity=Severity.RED,
            message=f"{contracts_dir} not found",
            next_step="Verify repo layout.",
            evidence={"contracts_dir": str(contracts_dir)},
        )
    try:
        out = subprocess.run(
            ["forge", "build", "--root", str(contracts_dir)],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return CheckResult(
            name="contracts_buildable",
            severity=Severity.RED,
            message="forge build timed out (>120s)",
            next_step="Run `cd contracts && forge build` manually.",
            evidence={},
        )
    if out.returncode != 0:
        return CheckResult(
            name="contracts_buildable",
            severity=Severity.RED,
            message="forge build failed",
            next_step="Run `cd contracts && forge build` to inspect errors.",
            evidence={
                "stderr": out.stderr.strip()[:500],
                "stdout": out.stdout.strip()[:200],
            },
        )
    return CheckResult(
        name="contracts_buildable",
        severity=Severity.GREEN,
        message="forge build succeeded",
        evidence={},
    )


# ---------------------------------------------------------------------------
# Output writability
# ---------------------------------------------------------------------------


def check_demo_output_writable(
    output_path: Optional[str] = None,
) -> CheckResult:
    """Can we write to ``scripts/demo_output.jsonl``?

    The demo truncates and re-writes this file on each run. If the parent
    directory isn't writable, the demo will crash mid-run.
    """
    path = Path(output_path) if output_path else REPO_ROOT / "scripts" / "demo_output.jsonl"
    parent = path.parent
    if not parent.exists():
        try:
            parent.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            return CheckResult(
                name="demo_output",
                severity=Severity.RED,
                message=f"Cannot create {parent}: {type(e).__name__}",
                next_step=f"Verify filesystem permissions on {parent}",
                evidence={"path": str(path)},
            )
    if not os.access(parent, os.W_OK):
        return CheckResult(
            name="demo_output",
            severity=Severity.RED,
            message=f"{parent} not writable",
            next_step=f"Verify filesystem permissions on {parent}",
            evidence={"path": str(path)},
        )
    return CheckResult(
        name="demo_output",
        severity=Severity.GREEN,
        message=f"Demo output writable ({path})",
        evidence={"path": str(path)},
    )
