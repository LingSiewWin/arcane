"""alice — the seasoned agent (Slice 5A).

Alice owns a real ``MemoryService`` seeded with N templated trade-reasoning
strings (real MiniLM embeddings — same template family as
``bench/audit_memory_real_text.py``) plus three pinned constitution rules.
She exposes the memory behind Slice 4's ``DarkPoolServer`` (x402 paywall).

Two surfaces:

1. **Object-oriented (Slice 5A primary)** — what Slice 5A's brief specifies.

       alice = Alice(corpus_size=5000, port=8001)
       alice.bootstrap()              # seeds memory + starts dark pool
       alice.dark_pool_url            # "http://127.0.0.1:8001"
       alice.pinned_root              # bytes32 — to be anchored on Arc
       alice.client                   # FastAPI TestClient (in-process)

2. **Module helpers (Slice 5D shim)** — preserved from the earlier stub so
   ``scripts/demo_e2e.py`` keeps working.

       app = build_app(AliceConfig(memory_path="/tmp/alice.mem", ...))
       proc, url = start_alice_subprocess(cfg, port=8401)

The two paths share the same ``MemoryService`` schema. Boot path:
  * cold:  generate corpus, embed via sentence-transformers, save to
           ``/tmp/alice.mem``
  * warm:  load ``/tmp/alice.mem`` directly; skip the embedding cost
"""

from __future__ import annotations

import logging
import os
import random
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np
from eth_account import Account
from fastapi.testclient import TestClient

from agents.dark_pool import DarkPoolServer
from agents.memory_service import MemoryService, hash_to_vec
from agents.nonce_store import InMemoryNonceStore

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants — templates mirror bench/audit_memory_real_text.py so the 92%
# recall number from the audit transfers over.
# ---------------------------------------------------------------------------

DEFAULT_MEM_PATH = "/tmp/alice.mem"
DEFAULT_EMBED_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
DEFAULT_EMBED_DIM = 384
DEFAULT_PRICE_USDC = "0.001"
DEFAULT_USDC_ADDRESS = "0x3600000000000000000000000000000000000000"
DEFAULT_CHAIN_ID = 5042002  # Arc Testnet

TOKENS = ["SOL", "BONK", "WIF", "JTO", "JUP", "PYTH", "MOG", "FARTCOIN", "PUMP", "RAY"]
SIDES = ["buy", "sell", "short", "long"]
SIGNALS = [
    "funding rate flipped negative",
    "social sentiment spike on twitter",
    "whale wallet accumulated 5M tokens",
    "perp open interest jumped 40%",
    "moving average golden cross",
    "RSI dropped below 30",
    "liquidity imbalance on Jupiter",
    "exchange inflow spiked",
    "stablecoin dominance dropped",
    "BTC dominance broke 50",
    "AI sector outperformed by 8%",
    "memecoin index up 12% in 24h",
]
VENUES = ["Jupiter", "Drift", "Phoenix", "Raydium", "Hyperliquid", "DriftV2", "Zeta", "Mango"]
RISK_LEVELS = ["low", "medium", "high"]
SIZES_USDC = [10, 25, 50, 100, 250, 500, 1000]


# ---------------------------------------------------------------------------
# Constitution rule shape — Alice pins these into the memory's pinned slot.
# Same kinds as ``contracts/src/ConstitutionRegistry.sol``.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ConstitutionRule:
    """A constitution rule pinned into Alice's memory.

    ``kind`` is the human-readable string label
    (MAX_TRADE_SIZE / VENUE_BLACKLIST / NO_UNAUDITED_CONTRACTS / ...).
    ``params`` is a free-form dict; the orchestrator + Bob consult it
    when shaping calldata.
    """

    rule_id: str
    kind: str
    params: dict

    def canonical_text(self) -> str:
        # Stable string form for embedding the rule + sorting keys so the
        # embedding doesn't drift between processes.
        kv = ",".join(f"{k}={self.params[k]}" for k in sorted(self.params))
        return f"constitution rule {self.kind} ({kv})"


DEFAULT_PINNED_RULES: tuple[ConstitutionRule, ...] = (
    ConstitutionRule(
        rule_id="MAX_TRADE_SIZE_1USDC",
        kind="MAX_TRADE_SIZE",
        params={"max_usdc": 1.0},
    ),
    ConstitutionRule(
        rule_id="VENUE_BLACKLIST_DEFAULT",
        kind="VENUE_BLACKLIST",
        # Deterministic sentinel; the orchestrator overrides for the demo.
        params={"venues": ["0x000000000000000000000000000000000000dEaD"]},
    ),
    ConstitutionRule(
        rule_id="NO_UNAUDITED_CONTRACTS_DEFAULT",
        kind="NO_UNAUDITED_CONTRACTS",
        # Empty whitelist == feature disabled per the Solidity hook. The
        # orchestrator overrides this to enable the check for the demo.
        params={"whitelist": []},
    ),
)


# ---------------------------------------------------------------------------
# Alice
# ---------------------------------------------------------------------------


@dataclass
class Alice:
    """Seasoned agent: seeded memory + dark-pool host."""

    corpus_size: int = 5000
    embedding_model: str = DEFAULT_EMBED_MODEL
    embedding_dim: int = DEFAULT_EMBED_DIM
    mem_path: str = DEFAULT_MEM_PATH
    port: int = 8001
    seed: int = 2026
    pinned_rules: tuple[ConstitutionRule, ...] = DEFAULT_PINNED_RULES
    payment_recipient: Optional[str] = None  # filled by bootstrap() if None
    price_usdc: str = DEFAULT_PRICE_USDC
    usdc_address: str = DEFAULT_USDC_ADDRESS
    chain_id: int = DEFAULT_CHAIN_ID

    # Populated by bootstrap()
    memory: Optional[MemoryService] = field(default=None, init=False)
    server: Optional[DarkPoolServer] = field(default=None, init=False)
    client: Optional[TestClient] = field(default=None, init=False)
    account: Optional[object] = field(default=None, init=False)  # eth_account.Account
    corpus: list[tuple[str, str]] = field(default_factory=list, init=False)
    bootstrapped: bool = field(default=False, init=False)
    _bootstrap_lock: threading.Lock = field(
        default_factory=threading.Lock, init=False, repr=False
    )

    # ---- Public API ----------------------------------------------------

    def bootstrap(self, force_rebuild: bool = False) -> None:
        """Idempotent: seeds memory (or loads cached), pins rules, builds
        the dark-pool app + in-process test client. Re-runs are no-ops
        unless ``force_rebuild=True``."""
        with self._bootstrap_lock:
            if self.bootstrapped and not force_rebuild:
                return

            # 1. Payment recipient — a real EOA Alice controls. The address
            #    is what x402 clients pay to.
            if self.account is None:
                self.account = Account.create()
            if self.payment_recipient is None:
                self.payment_recipient = self.account.address

            # 2. Memory — cold rebuild if requested or cache missing / wrong size.
            cache_ok = (
                not force_rebuild
                and Path(self.mem_path).exists()
            )
            if cache_ok:
                log.info("Alice: loading memory cache from %s", self.mem_path)
                # A cache in an incompatible/old format (e.g. a pre-v3 npz, or a
                # corrupt file) must trigger a rebuild — never a hard crash.
                try:
                    cached = MemoryService.load(self.mem_path)
                except ValueError as exc:
                    log.info(
                        "Alice: cache at %s unreadable (%s) → rebuilding",
                        self.mem_path,
                        exc,
                    )
                    cached = None
                # Expect corpus_size working entries + len(pinned_rules) pinned.
                expected_min = self.corpus_size + len(self.pinned_rules)
                if cached is not None and len(cached) >= expected_min:
                    self.memory = cached
                else:
                    if cached is not None:
                        log.info(
                            "Alice: cache has %d entries < expected %d → rebuilding",
                            len(cached),
                            expected_min,
                        )
                    self._build_memory()
            else:
                self._build_memory()

            # 3. Dark pool host — use the in-memory nonce store for tests
            #    (no sqlite file collisions across runs).
            self.server = DarkPoolServer(
                memory=self.memory,
                price_per_query_usdc=self.price_usdc,
                payment_recipient=self.payment_recipient,
                arc_chain_id=self.chain_id,
                usdc_address=self.usdc_address,
                nonce_store=InMemoryNonceStore(),
            )
            self.client = TestClient(self.server.app)
            self.bootstrapped = True

    @property
    def dark_pool_url(self) -> str:
        # The real-network URL the user would dial. Tests use ``self.client``
        # directly to skip the network; this is just the address uvicorn
        # would bind to.
        return f"http://127.0.0.1:{self.port}"

    @property
    def pinned_root(self) -> bytes:
        """Merkle root of Alice's pinned constitution slot (bytes32).
        Stable across save/load. Same value the on-chain anchor uses."""
        if self.memory is None:
            raise RuntimeError("Alice not bootstrapped")
        return self.memory.pinned_merkle_root()

    @property
    def address(self) -> str:
        if self.account is None:
            raise RuntimeError("Alice not bootstrapped")
        return self.account.address

    # ---- Internals -----------------------------------------------------

    def _build_memory(self) -> None:
        log.info(
            "Alice: cold rebuild — corpus_size=%d model=%s",
            self.corpus_size,
            self.embedding_model,
        )
        self.corpus = make_corpus(self.corpus_size, seed=self.seed)

        # Lazy import — sentence_transformers drags torch with it (slow).
        from sentence_transformers import SentenceTransformer  # noqa: WPS433

        t0 = time.time()
        model = SentenceTransformer(self.embedding_model)
        log.info("Alice: model loaded in %.1fs", time.time() - t0)

        t0 = time.time()
        embs = model.encode(
            [text for _, text in self.corpus],
            batch_size=128,
            normalize_embeddings=True,
            show_progress_bar=False,
        ).astype(np.float32)
        log.info(
            "Alice: embedded %d texts in %.1fs (shape=%s)",
            self.corpus_size,
            time.time() - t0,
            embs.shape,
        )
        if embs.shape[1] != self.embedding_dim:
            raise RuntimeError(
                f"embedding dim mismatch: got {embs.shape[1]}, "
                f"expected {self.embedding_dim}"
            )

        mem = MemoryService(dim=self.embedding_dim, seed=self.seed)

        t0 = time.time()
        for (tid, text), emb in zip(self.corpus, embs):
            mem.add(
                trace_id=tid,
                vec=emb,
                kind="working",
                payload={"text": text},
            )
        log.info(
            "Alice: inserted %d entries in %.2fs",
            self.corpus_size,
            time.time() - t0,
        )

        # Pin constitution rules. We hash-to-vec the rule's canonical text
        # so pinning is deterministic without re-running the embedding model
        # (the rules don't need semantic neighbours — they need to be
        # non-evictable and Merkle-anchorable).
        for rule in self.pinned_rules:
            vec = hash_to_vec(
                rule.canonical_text(), dim=self.embedding_dim, seed=self.seed
            )
            mem.add(
                trace_id=f"pinned:{rule.rule_id}",
                vec=vec,
                kind="pinned",
                pinned=True,
                payload={
                    "rule_id": rule.rule_id,
                    "kind": rule.kind,
                    "params": rule.params,
                    "text": rule.canonical_text(),
                },
            )

        self.memory = mem
        os.makedirs(os.path.dirname(self.mem_path) or ".", exist_ok=True)
        mem.save(self.mem_path)
        log.info("Alice: saved snapshot → %s", self.mem_path)


# ---------------------------------------------------------------------------
# Corpus generator — same template family as bench/audit_memory_real_text.py.
# Exposed for direct use by ``agents/seed_alice.py``.
# ---------------------------------------------------------------------------


def make_corpus(n: int, seed: int = 2026) -> list[tuple[str, str]]:
    """Return ``n`` (trace_id, text) pairs of structured trade-reasoning.

    Each entry is a single English sentence — enough for MiniLM to produce
    a meaningful embedding.
    """
    rng = random.Random(seed)
    out: list[tuple[str, str]] = []
    for i in range(n):
        token = rng.choice(TOKENS)
        side = rng.choice(SIDES)
        signal = rng.choice(SIGNALS)
        venue = rng.choice(VENUES)
        risk = rng.choice(RISK_LEVELS)
        size = rng.choice(SIZES_USDC)
        text = (
            f"{side} {token} on {venue} size {size} USDC because {signal} "
            f"risk {risk} conviction 0.{rng.randint(50, 95)}"
        )
        out.append((f"t{i:05d}", text))
    return out


# ===========================================================================
# Slice 5D shim — preserved from the earlier stub so demo_e2e.py keeps
# working. These functions construct the same FastAPI app from a config
# but read the memory from disk rather than rebuilding it.
# ===========================================================================


@dataclass
class AliceConfig:
    """Configuration for an Alice subprocess instance."""

    memory_path: str = DEFAULT_MEM_PATH
    payment_recipient: str = "0x000000000000000000000000000000000000A11C"
    price_per_query_usdc: str = DEFAULT_PRICE_USDC
    arc_chain_id: int = DEFAULT_CHAIN_ID
    usdc_address: str = DEFAULT_USDC_ADDRESS
    use_in_memory_nonces: bool = True


def build_app(cfg: AliceConfig):
    """Build an Alice FastAPI app, loading the memory from disk."""
    if not os.path.exists(cfg.memory_path):
        raise FileNotFoundError(
            f"alice memory not found at {cfg.memory_path}. "
            f"Run `python -m agents.seed_alice` first."
        )
    mem = MemoryService.load(cfg.memory_path)
    nonce_store = InMemoryNonceStore() if cfg.use_in_memory_nonces else None
    server = DarkPoolServer(
        memory=mem,
        price_per_query_usdc=cfg.price_per_query_usdc,
        payment_recipient=cfg.payment_recipient,
        arc_chain_id=cfg.arc_chain_id,
        usdc_address=cfg.usdc_address,
        nonce_store=nonce_store,
    )
    return server.app


def _pick_free_port(start: int = 8401, end: int = 8500) -> int:
    """Find an unused TCP port in ``[start, end)``."""
    for port in range(start, end):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError(f"no free port in [{start},{end})")


def start_alice_subprocess(
    cfg: AliceConfig,
    *,
    port: Optional[int] = None,
    startup_timeout: float = 10.0,
) -> tuple[subprocess.Popen, str]:
    """Spawn a uvicorn process serving Alice. Blocks until TCP open.
    Returns ``(proc, base_url)``. Caller owns ``proc.terminate()``."""
    if port is None:
        port = _pick_free_port()
    url = f"http://127.0.0.1:{port}"

    env = os.environ.copy()
    env["ALICE_MEMORY_PATH"] = cfg.memory_path
    env["ALICE_RECIPIENT"] = cfg.payment_recipient
    env["ALICE_PRICE_USDC"] = cfg.price_per_query_usdc
    env["ALICE_CHAIN_ID"] = str(cfg.arc_chain_id)
    env["ALICE_USDC_ADDR"] = cfg.usdc_address

    cmd = [
        sys.executable,
        "-m",
        "uvicorn",
        "agents.alice:_subprocess_app",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--log-level",
        "warning",
    ]
    proc = subprocess.Popen(cmd, env=env)

    deadline = time.time() + startup_timeout
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.2)
            try:
                s.connect(("127.0.0.1", port))
                return proc, url
            except OSError:
                time.sleep(0.05)
    proc.terminate()
    raise RuntimeError(
        f"alice did not start within {startup_timeout}s on port {port}"
    )


def _build_subprocess_app():
    """Lazily build the app from env vars (uvicorn entry point)."""
    cfg = AliceConfig(
        memory_path=os.environ.get("ALICE_MEMORY_PATH", DEFAULT_MEM_PATH),
        payment_recipient=os.environ.get(
            "ALICE_RECIPIENT", "0x000000000000000000000000000000000000A11C"
        ),
        price_per_query_usdc=os.environ.get("ALICE_PRICE_USDC", DEFAULT_PRICE_USDC),
        arc_chain_id=int(os.environ.get("ALICE_CHAIN_ID", str(DEFAULT_CHAIN_ID))),
        usdc_address=os.environ.get("ALICE_USDC_ADDR", DEFAULT_USDC_ADDRESS),
    )
    return build_app(cfg)


# Only evaluated when uvicorn imports the module with env vars populated;
# plain ``import agents.alice`` does NOT touch the filesystem.
_subprocess_app = None
if os.environ.get("ALICE_MEMORY_PATH"):
    _subprocess_app = _build_subprocess_app()


# ---------------------------------------------------------------------------
# Module entrypoint — handy for ad-hoc smoke tests.
# ---------------------------------------------------------------------------


if __name__ == "__main__":  # pragma: no cover
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    a = Alice()
    a.bootstrap()
    print(
        f"Alice bootstrapped. entries={len(a.memory)} "
        f"pinned_root=0x{a.pinned_root.hex()}"
    )
    print(f"  payment_recipient={a.payment_recipient}")
    print(f"  dark_pool_url={a.dark_pool_url}")
