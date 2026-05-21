"""Pytest collection hook for the agents test suite.

Previously this file pre-seeded ``/tmp/alice.mem`` so that the module-level
``app = _build_default_app()`` evaluation in ``agents.dark_pool`` did not
crash on a fresh checkout. That workaround is no longer needed: the dark
pool now exposes ``app`` lazily via ``__getattr__`` (Bug 1 fix), so
importing ``agents.dark_pool`` has zero filesystem side effects.

This conftest is intentionally a no-op, kept so pytest still discovers the
``agents/tests`` directory as a package.
"""

from __future__ import annotations
