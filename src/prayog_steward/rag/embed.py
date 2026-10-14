"""Embedders and chunking (ADR-S06, ADR-S09).

``HashingEmbedder`` is the default: deterministic, dependency-free and good at the lexical
matching error diagnosis mostly needs (exception names, package names, error phrases).
``SentenceTransformerEmbedder`` is the semantic upgrade (``pip install prayog-steward[rag]``).
Both run on the server's CPU: no data leaves the server and no GPU from the scheduled
pool is used (NFR-PERF-001).
"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Sequence
from itertools import pairwise
from typing import Any, Protocol

_WORD = re.compile(r"[A-Za-z][a-z]+|[A-Z]+(?![a-z])|\d+(?:\.\d+)*")


class Embedder(Protocol):
    name: str

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """Embed passages (documents, chunks)."""
        ...

    def embed_query(self, texts: Sequence[str]) -> list[list[float]]:
        """Embed search queries. Some models (BGE) want an instruction prefix here."""
        ...


def _normalise(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in vec))
    return [v / norm for v in vec] if norm else vec


class HashingEmbedder:
    """Signed feature hashing of word unigrams and bigrams (camelCase and snake_case split)."""

    def __init__(self, dim: int = 384) -> None:
        self.dim = dim
        self.name = f"hashing-{dim}-v1"

    def _features(self, text: str) -> list[str]:
        words = [w.lower() for w in _WORD.findall(text)]
        return words + [f"{a}_{b}" for a, b in pairwise(words)]

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for text in texts:
            vec = [0.0] * self.dim
            for feature in self._features(text):
                digest = hashlib.blake2b(feature.encode(), digest_size=8).digest()
                h = int.from_bytes(digest, "little")
                vec[h % self.dim] += 1.0 if (h >> 63) & 1 else -1.0
            out.append(_normalise(vec))
        return out

    def embed_query(self, texts: Sequence[str]) -> list[list[float]]:
        return self.embed(texts)


class SentenceTransformerEmbedder:
    def __init__(self, model: str = "BAAI/bge-small-en-v1.5") -> None:
        self._model_name = model
        self._model: Any = None  # loaded on first use: building services stays cheap
        self.name = f"st:{model}"
        # BGE v1.5 retrieves best with this instruction on queries (not on passages).
        self._query_prefix = (
            "Represent this sentence for searching relevant passages: "
            if "bge" in model.lower() and "en" in model.lower()
            else ""
        )

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if self._model is None:
            from sentence_transformers import SentenceTransformer

            self._model = SentenceTransformer(self._model_name, device="cpu")
        vectors = self._model.encode(list(texts), normalize_embeddings=True, batch_size=16)
        return [[float(x) for x in v] for v in vectors]

    def embed_query(self, texts: Sequence[str]) -> list[list[float]]:
        return self.embed([self._query_prefix + t for t in texts])


def sentence_transformers_available() -> bool:
    from importlib.util import find_spec

    return find_spec("sentence_transformers") is not None


def build_embedder(kind: str) -> Embedder:
    """``hashing``, ``st:<model>``, or ``auto`` (BGE-base if sentence-transformers is
    installed, hashing otherwise). Both ingest and query must use the same embedder."""
    if kind == "auto":
        kind = "st:BAAI/bge-base-en-v1.5" if sentence_transformers_available() else "hashing"
    if kind == "hashing":
        return HashingEmbedder()
    if kind.startswith("st:"):
        return SentenceTransformerEmbedder(kind.removeprefix("st:"))
    raise ValueError(f"unknown embedder {kind!r}; use 'hashing' or 'st:<model>'")


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def chunk(text: str, max_chars: int = 800, overlap_lines: int = 2) -> list[str]:
    """Split on line boundaries into chunks of at most ``max_chars`` (long lines are cut),
    carrying a couple of lines of overlap so a traceback is not split from its message."""
    lines: list[str] = []
    for line in text.splitlines():
        while len(line) > max_chars:
            lines.append(line[:max_chars])
            line = line[max_chars:]
        lines.append(line)
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for line in lines:
        if current and size + len(line) + 1 > max_chars:
            chunks.append("\n".join(current))
            current = current[-overlap_lines:] if overlap_lines else []
            size = sum(len(c) + 1 for c in current)
            if size + len(line) + 1 > max_chars:
                current, size = [], 0
        current.append(line)
        size += len(line) + 1
    if any(c.strip() for c in current):
        chunks.append("\n".join(current))
    return [c for c in chunks if c.strip()]
