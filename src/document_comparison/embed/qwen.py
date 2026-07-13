"""Qwen 向量引擎:走 OpenAI 兼容的 /v1/embeddings(vLLM / SGLang 自建服务)。

适用于生产环境自建 Qwen3-Embedding-0.6B(或任意 OpenAI 兼容 embedding 服务)。
OCR 与 embed 通常是两个独立推理服务,故本引擎独立配置 base / key / model。

设计要点:
- embed_batch 一次请求多 input,对齐阶段算 word 侧全部条款时降 RPC 数。
- similarity 用余弦(点积除以模长);vLLM 默认返回归一化向量,本地再归一是保险。
- 本地 LRU 缓存:对齐阶段会重复 embed 同一文本(见 align/matcher.py 与
  report/builder.py 的 number 配对重算),缓存命中可显著降 RPC。
- 瞬态重试(超时 / 429 / 5xx),模式同 ocr/llm.py。
"""
from __future__ import annotations

import logging
import random
import time
from functools import lru_cache
from typing import Any

import httpx

from ..config import settings

logger = logging.getLogger(__name__)


class QwenEmbedding:
    """通过 OpenAI 兼容 /v1/embeddings 的向量引擎。

    配置来源(settings,由 llm_config.json 覆盖):
    - embed_api_base    API 根地址(如 http://vllm-embed:8000/v1)
    - embed_api_key     API Key(本地 vLLM 可留空)
    - embed_model       模型名(如 Qwen3-Embedding-0.6B)
    - embed_timeout     单次请求读取超时秒
    """

    def __init__(
        self,
        api_base: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        timeout: float | None = None,
        max_retries: int = 2,
    ) -> None:
        self.api_base = api_base if api_base is not None else settings.embed_api_base
        self.api_key = api_key if api_key is not None else settings.embed_api_key
        self.model = model or settings.embed_model
        self.timeout = timeout if timeout is not None else settings.embed_timeout
        self.max_retries = max_retries
        if not self.api_base:
            raise ValueError(
                "qwen embed 引擎未配置 embed_api_base,"
                "请在设置页填写向量服务地址"
            )
        if not self.model:
            raise ValueError(
                "qwen embed 引擎未配置 embed_model,"
                "请在设置页填写向量模型名"
            )
        # 预热 certifi CA bundle(同 ocr/llm.py,规避并发竞态)
        import certifi
        certifi.where()

    # —— EmbeddingEngine 接口 ——
    def embed(self, text: str) -> Any:
        return self._embed_cached(text)

    def similarity(self, vec_a: Any, vec_b: Any) -> float:
        import math

        if not vec_a or not vec_b:
            return 0.0
        dot = sum(a * b for a, b in zip(vec_a, vec_b))
        na = math.sqrt(sum(a * a for a in vec_a))
        nb = math.sqrt(sum(b * b for b in vec_b))
        if na == 0 or nb == 0:
            return 0.0
        return dot / (na * nb)

    # —— 批量(降低 RPC 数;绕过单条缓存)——
    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        data = self._post_embeddings(texts)
        # OpenAI 协议:{"data": [{"embedding": [...]}, ...]},顺序与 input 对齐
        return [item["embedding"] for item in data]

    # —— 内部 ——
    @lru_cache(maxsize=2048)
    def _embed_cached(self, text: str) -> tuple[float, ...]:
        """单条 embed + 本地缓存。返回 tuple(可哈希,缓存友好)。

        对齐层与 similarity 只读取向量数值,tuple 与 list 等价。
        """
        data = self._post_embeddings([text])
        vec = data[0]["embedding"]
        return tuple(vec)

    def _post_embeddings(self, inputs: list[str]) -> list[dict]:
        url = self.api_base.rstrip("/") + "/embeddings"
        payload: dict[str, Any] = {"model": self.model, "input": inputs}
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        timeout = httpx.Timeout(self.timeout, connect=10.0)

        last_exc: Exception | None = None
        with httpx.Client(timeout=timeout) as client:
            for attempt in range(self.max_retries + 1):
                try:
                    resp = client.post(url, json=payload, headers=headers)
                except httpx.TransportError as exc:
                    last_exc = exc
                else:
                    if resp.status_code == 429 or resp.status_code >= 500:
                        last_exc = httpx.HTTPStatusError(
                            f"服务端瞬态错误:HTTP {resp.status_code}",
                            request=resp.request,
                            response=resp,
                        )
                    else:
                        resp.raise_for_status()
                        return resp.json()["data"]
                if attempt < self.max_retries:
                    time.sleep(min(2 ** attempt, 8) + random.random())
        assert last_exc is not None
        raise last_exc
