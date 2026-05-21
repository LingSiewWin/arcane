#!/usr/bin/env python3
"""seed_alice — convenience wrapper around agents.seed_alice.

Exists so the demo runbook reads cleanly:
    python -m scripts.seed_alice --out /tmp/alice.mem
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from agents.seed_alice import seed_alice  # noqa: E402


def _cli() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--out", default="/tmp/alice.mem")
    p.add_argument("--n", type=int, default=200)
    args = p.parse_args()
    path = seed_alice(out_path=args.out, n=args.n)
    print(path)


if __name__ == "__main__":
    _cli()
