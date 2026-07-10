"""语义向量引擎(对齐兜底,§4)。

默认 `mock`(字符袋余弦,反映字面相似度);生产切 `bge`(bge-m3)。
由 DC_EMBED_BACKEND 控制。
"""
from __future__ import annotations

from ..config import settings
from .base import EmbeddingEngine


def get_embed_engine(backend: str | None = None) -> EmbeddingEngine:
    backend = backend or settings.embed_backend
    if backend == "bge":
        from .bge import BgeEmbedding

        return BgeEmbedding()
    if backend != "mock":
        raise ValueError(f"未知 embed backend: {backend}")
    from .mock import MockEmbedding

    return MockEmbedding()


__all__ = ["EmbeddingEngine", "get_embed_engine"]
