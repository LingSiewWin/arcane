"""embedder.py — shared MiniLM embedder with a deterministic fallback.

Lifted verbatim out of ``agents/registry_api.py`` so duel agents (and anything
else) can embed text WITHOUT importing the FastAPI/pydantic web stack. The logic
is unchanged: real MiniLM via ``sentence-transformers`` (lazily loaded behind a
thread lock), or the deterministic ``hash_to_vec`` path when ``model_name`` is
``None`` — used by tests so we don't pay torch's import cost or require model
weights. See ``agents/bob.py`` for the same pattern.
"""

from __future__ import annotations

import threading
from typing import Optional

import numpy as np

from agents.memory_service import hash_to_vec

# Same embedder family Alice/Bob/the dark pool use. Real MiniLM by default; the
# deterministic ``hash_to_vec`` fallback keeps callers runnable (and tests fast)
# without dragging torch in.
DEFAULT_EMBED_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
DEFAULT_EMBED_DIM = 384


class Embedder:
    """Lazily-loaded MiniLM embedder with a deterministic fallback.

    ``model_name=None`` forces the deterministic ``hash_to_vec`` path —
    used by tests so we don't pay torch's import cost or require model weights.
    """

    def __init__(
        self,
        model_name: Optional[str] = DEFAULT_EMBED_MODEL,
        dim: int = DEFAULT_EMBED_DIM,
        seed: int = 0,
    ) -> None:
        self.model_name = model_name
        self.dim = int(dim)
        self.seed = int(seed)
        self._model = None
        self._lock = threading.Lock()

    def embed(self, text: str) -> np.ndarray:
        if self.model_name is None:
            return hash_to_vec(text, dim=self.dim, seed=self.seed)
        with self._lock:
            if self._model is None:
                from sentence_transformers import SentenceTransformer

                self._model = SentenceTransformer(self.model_name)
        emb = self._model.encode(
            [text], normalize_embeddings=True, show_progress_bar=False
        )[0].astype(np.float32)
        if emb.shape[0] != self.dim:
            raise RuntimeError(
                f"embedding dim mismatch: got {emb.shape[0]}, want {self.dim}"
            )
        return emb
