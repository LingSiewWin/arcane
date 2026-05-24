#!/usr/bin/env python3
"""preflight — green-light validator for AgoraHack live broadcast.

Validates EVERY precondition for ``scripts/demo_e2e.py --mode live`` BEFORE
any USDC is spent. Runs a battery of checks and either prints a green light
+ the exact go-live command, or a precise list of what's missing.

Usage::

    # Default: validate everything required for live mode
    python -m scripts.preflight

    # Same thing, explicit
    python -m scripts.preflight --mode live

    # Tolerant: only fail on RED (live mode minimum)
    python -m scripts.preflight --mode local

    # Strict: any YELLOW fails too
    python -m scripts.preflight --strict

Exit codes::

    0   GREEN — all checks pass. Safe to broadcast.
    1   YELLOW — at least one check warned. Live mode may work but
        deviates from the audited path. Re-run with --strict to enforce.
    2   RED — at least one check failed. Live mode WILL fail. Don't run it.

The checker is intentionally paranoid. Each check is a small pure function in
``scripts/preflight_checks.py``; ``preflight.py`` is just the orchestrator
and reporter.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Callable, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts.preflight_checks import (  # noqa: E402
    ARC_TESTNET_CHAIN_ID,
    CheckResult,
    FAUCET_URL,
    Severity,
    check_alice_memory_seeded,
    check_anvil_available,
    check_cast_available,
    check_contracts_compiled,
    check_demo_output_writable,
    check_deployer_address,
    check_deployer_key,
    check_env_loaded,
    check_forge_available,
    check_no_old_nonce_db,
    check_node_modules,
    check_python_venv,
    check_rpc,
    check_usdc_balance,
    resolve_rpc,
)


SEVERITY_PREFIX = {
    Severity.GREEN: "[GREEN]",
    Severity.YELLOW: "[YELLOW]",
    Severity.RED: "[RED]  ",
}


def _print_result(r: CheckResult, *, use_color: bool) -> None:
    """Print one check's result to stdout."""
    prefix = SEVERITY_PREFIX[r.severity]
    if use_color:
        colors = {
            Severity.GREEN: "\033[32m",
            Severity.YELLOW: "\033[33m",
            Severity.RED: "\033[31m",
        }
        reset = "\033[0m"
        prefix = f"{colors[r.severity]}{prefix}{reset}"
    print(f"{prefix}  {r.message}")
    if r.severity is not Severity.GREEN and r.next_step:
        for line in r.next_step.splitlines():
            print(f"          -> {line}")


def collect_checks(mode: str) -> list[CheckResult]:
    """Run all checks in dependency order and return their results.

    The order matters: we need the deployer address before we can query its
    USDC balance, and we need a working RPC before either matters.

    ``mode`` is ``"live"`` or ``"local"``. In local mode we still run all
    checks but downgrade some RED outcomes (e.g. zero USDC) to YELLOW because
    they don't block anvil-fork demos.
    """
    results: list[CheckResult] = []

    # --- Stage 1: tools + filesystem ----------------------------------------
    results.append(check_anvil_available())
    results.append(check_forge_available())
    results.append(check_cast_available())
    results.append(check_python_venv())
    results.append(check_node_modules())
    results.append(check_contracts_compiled())
    results.append(check_demo_output_writable())
    results.append(check_alice_memory_seeded())
    results.append(check_no_old_nonce_db())

    # --- Stage 2: env + RPC -------------------------------------------------
    env_check = check_env_loaded()
    results.append(env_check)

    rpc_url = resolve_rpc()
    if mode == "live":
        rpc_check = check_rpc(rpc_url) if rpc_url else env_check
        # ``rpc_check`` may already equal env_check; only append if it's a
        # different object so we don't duplicate the message.
        if rpc_check is not env_check:
            results.append(rpc_check)
    else:
        # local mode: RPC is nice-to-have for anvil --fork-url but not required.
        if rpc_url:
            results.append(check_rpc(rpc_url))

    # --- Stage 3: deployer key + USDC (live only) ---------------------------
    if mode == "live":
        key_check = check_deployer_key()
        results.append(key_check)

        addr_check = check_deployer_address()
        results.append(addr_check)

        # Only attempt balance check if we have both an address and a live RPC.
        deployer_addr = addr_check.evidence.get("address")
        rpc_green = any(r.name == "rpc" and r.ok for r in results)
        if deployer_addr and rpc_url and rpc_green:
            results.append(
                check_usdc_balance(deployer_addr, rpc_url, min_usdc=2.0)
            )
        else:
            # Synthesize a deferred result so the SUMMARY still mentions it.
            results.append(
                CheckResult(
                    name="usdc_balance",
                    severity=Severity.RED,
                    message=(
                        "USDC balance check skipped (need deployer address "
                        "+ reachable RPC first)"
                    ),
                    next_step="Resolve the checks above and re-run preflight.",
                    evidence={},
                )
            )

    return results


def _go_live_command(deployer_addr: Optional[str]) -> str:
    py = REPO_ROOT / "agents" / ".venv" / "bin" / "python"
    account = os.environ.get("DEPLOYER_ACCOUNT", "").strip()
    if account:
        # Preferred path: encrypted keystore. The key never sits in an env
        # var — it's decrypted in-process from ~/.foundry/keystores/<account>.
        return (
            "    RPC=$RPC \\\n"
            f"    {py} -m scripts.demo_e2e \\\n"
            f"        --mode live --account {account} --yes-i-understand\n"
            "    # password: enter when prompted, or set KEYSTORE_PASSWORD"
        )
    return (
        "    RPC=$RPC \\\n"
        "    DEPLOYER_PK=$DEPLOYER_PK \\\n"
        f"    {py} -m scripts.demo_e2e \\\n"
        "        --mode live --yes-i-understand"
    )


def _print_summary(
    results: list[CheckResult],
    *,
    mode: str,
    strict: bool,
    use_color: bool,
) -> int:
    """Print SUMMARY block and return the exit code."""
    n_green = sum(1 for r in results if r.severity is Severity.GREEN)
    n_yellow = sum(1 for r in results if r.severity is Severity.YELLOW)
    n_red = sum(1 for r in results if r.severity is Severity.RED)

    print()
    print(f"SUMMARY: {n_green} green, {n_yellow} yellow, {n_red} red.")
    print()

    # Resolve deployer address if known (for the go-live banner).
    deployer_addr: Optional[str] = None
    for r in results:
        if r.name == "deployer_address" and r.severity is Severity.GREEN:
            deployer_addr = r.evidence.get("address")
            break

    if n_red > 0:
        msg = "Cannot proceed to live mode."
        if use_color:
            msg = f"\033[31m{msg}\033[0m"
        print(msg)
        print()
        print("To resolve:")
        for r in results:
            if r.severity is Severity.RED and r.next_step:
                print(f"  - {r.message}")
                for line in r.next_step.splitlines():
                    print(f"      {line}")
        print()
        print("To re-run:")
        print(f"    python -m scripts.preflight --mode {mode}")
        return 2

    if n_yellow > 0:
        msg = (
            "Live mode is technically possible but deviates from the audited "
            "path. Re-run with --strict to enforce, or proceed with care."
        )
        if use_color:
            msg = f"\033[33m{msg}\033[0m"
        print(msg)
        print()
        if mode == "live":
            print("Go-live command (proceed with care):")
            print(_go_live_command(deployer_addr))
            print()
            if deployer_addr:
                print(f"After broadcast, verify your txs on:")
                print(f"    https://testnet.arcscan.app/address/{deployer_addr}")
        return 1 if strict else 0

    # All green.
    msg = "All checks pass."
    if use_color:
        msg = f"\033[32m{msg}\033[0m"
    print(msg)
    print()
    if mode == "live":
        print("Safe to broadcast. Run:")
        print()
        print(_go_live_command(deployer_addr))
        print()
        if deployer_addr:
            print("After broadcast, verify on the explorer:")
            print(f"    https://testnet.arcscan.app/address/{deployer_addr}")
    else:
        print("Safe to run local demo:")
        print(f"    {REPO_ROOT / 'agents' / '.venv' / 'bin' / 'python'} -m scripts.demo_e2e --mode local")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(
        prog="preflight",
        description=(
            "Pre-flight checker for AgoraHack. Validates every precondition "
            "for `demo_e2e --mode live` BEFORE any USDC is spent."
        ),
    )
    p.add_argument(
        "--mode",
        choices=["local", "live"],
        default="live",
        help=(
            "Target mode. 'live' (default) checks DEPLOYER_PK + USDC balance + "
            "real RPC. 'local' only requires the anvil-fork prereqs."
        ),
    )
    p.add_argument(
        "--strict",
        action="store_true",
        help="Treat YELLOW warnings as failures (exit 1 instead of 0).",
    )
    p.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON to stdout instead of human output.",
    )
    p.add_argument(
        "--no-color",
        action="store_true",
        help="Disable ANSI color codes (useful in CI).",
    )
    args = p.parse_args(argv)

    use_color = (
        not args.no_color
        and sys.stdout.isatty()
        and os.environ.get("NO_COLOR", "") == ""
    )

    results = collect_checks(args.mode)

    if args.json:
        out = {
            "mode": args.mode,
            "strict": args.strict,
            "results": [
                {
                    "name": r.name,
                    "severity": r.severity.value,
                    "message": r.message,
                    "next_step": r.next_step,
                    "evidence": r.evidence,
                }
                for r in results
            ],
        }
        n_red = sum(1 for r in results if r.severity is Severity.RED)
        n_yellow = sum(1 for r in results if r.severity is Severity.YELLOW)
        out["summary"] = {
            "green": sum(1 for r in results if r.severity is Severity.GREEN),
            "yellow": n_yellow,
            "red": n_red,
        }
        print(json.dumps(out, indent=2, default=str))
        if n_red > 0:
            return 2
        if n_yellow > 0:
            return 1 if args.strict else 0
        return 0

    print("=== AgoraHack -- pre-flight check ===")
    print(f"mode: {args.mode}    chain id (Arc Testnet): {ARC_TESTNET_CHAIN_ID}")
    print()

    for r in results:
        _print_result(r, use_color=use_color)

    return _print_summary(
        results, mode=args.mode, strict=args.strict, use_color=use_color
    )


if __name__ == "__main__":
    sys.exit(main())
