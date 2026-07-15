"""语义向量引擎(对齐兜底,§4)。

- `mock`:字符袋余弦,反映字面相似度(测试 / 回退)
- `qwen`:走 OpenAI 兼容 /v1/embeddings(vLLM / SGLang 自建 Qwen3-Embedding)
- `bge`:bge-m3 本地模型(需下载)

由 llm_config.json 的 embed_backend 字段控制。
qwen/bge 在配置缺失(api_base / model 未填)时自动回退到 mock,
保证开箱即用;pipeline 会记录一次回退提示。
"""
from __future__ import annotations

import logging

from ..config import settings
from .base import EmbeddingEngine

logger = logging.getLogger(__name__)


def get_embed_engine(backend: str | None = None) -> EmbeddingEngine:
    backend = backend or settings.embed_backend
    if backend == "qwen":
        try:
            from .qwen import QwenEmbedding

            return QwenEmbedding()
        except ValueError as exc:
            logger.warning("qwen 向量引擎配置缺失(%s),回退到 mock", exc)
            from .mock import MockEmbedding

            return MockEmbedding()
    if backend == "bge":
        try:
            from .bge import BgeEmbedding

            return BgeEmbedding()
        except Exception as exc:  # noqa: BLE001 — bge 可能缺 FlagEmbedding 依赖
            logger.warning("bge 向量引擎不可用(%s),回退到 mock", exc)
            from .mock import MockEmbedding

            return MockEmbedding()
    if backend != "mock":
        raise ValueError(f"未知 embed backend: {backend}")
    from .mock import MockEmbedding

    return MockEmbedding()


def is_mock_engine(embed) -> bool:
    """判断当前引擎是否为 mock(用于阈值自适应:mock 用较低阈值)。"""
    from .mock import MockEmbedding

    return isinstance(embed, MockEmbedding)


__all__ = ["EmbeddingEngine", "get_embed_engine", "is_mock_engine"]
