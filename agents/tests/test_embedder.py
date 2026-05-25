"""Tests for ``agents/embedder.py`` — the shared MiniLM embedder.

These exercise ONLY the deterministic ``hash_to_vec`` fallback path
(``model_name=None``), so they run offline and fast without torch /
sentence-transformers or model weights. The real-model path is covered by the
heavier orchestrator/dark-pool tests.
"""

from __future__ import annotations

import numpy as np

from agents.embedder import DEFAULT_EMBED_MODEL, Embedder


def test_default_embed_model_is_minilm() -> None:
    assert DEFAULT_EMBED_MODEL == "sentence-transformers/all-MiniLM-L6-v2"


def test_offline_embed_shape_and_dtype() -> None:
    """``model_name=None`` => deterministic vector, no torch required."""
    vec = Embedder(model_name=None).embed("hello world")
    assert isinstance(vec, np.ndarray)
    assert vec.shape == (384,)
    assert vec.dtype == np.float32


def test_offline_embed_is_deterministic() -> None:
    """Same text + same seed => identical vector across instances."""
    a = Embedder(model_name=None, seed=0).embed("hello world")
    b = Embedder(model_name=None, seed=0).embed("hello world")
    assert np.allclose(a, b)


def test_different_text_gives_different_vector() -> None:
    e = Embedder(model_name=None)
    a = e.embed("hello world")
    b = e.embed("goodbye world")
    assert not np.allclose(a, b)


def test_custom_dim_is_respected() -> None:
    vec = Embedder(model_name=None, dim=128).embed("hello world")
    assert vec.shape == (128,)
    assert vec.dtype == np.float32
