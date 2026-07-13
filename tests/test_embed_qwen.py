"""Qwen 向量引擎测试:用 httpx.MockTransport 桩 /v1/embeddings。

不发起真实网络请求,不依赖外部服务。
"""
from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from document_comparison.embed.qwen import QwenEmbedding


def _mock_embeddings_handler(calls: list[dict]) -> Any:
    """构造 MockTransport handler:把收到的请求存入 calls,返回固定向量。

    按 input 文本哈希派发不同向量,使相似度可预测。
    """
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        inputs = body["input"]
        calls.append({"url": str(request.url), "model": body["model"], "inputs": inputs})
        data = []
        for i, text in enumerate(inputs):
            # 简单确定性向量:文本长度与首字符码点决定方向
            n = len(text)
            c = ord(text[0]) if text else 0
            data.append({"object": "embedding", "index": i, "embedding": [n, c, n + c]})
        return httpx.Response(200, json={"object": "list", "data": data})

    return handler


def _make_engine(handler, **kwargs) -> QwenEmbedding:
    transport = httpx.MockTransport(handler)
    engine = QwenEmbedding(
        api_base="http://embed.test/v1",
        api_key="sk-test",
        model="Qwen3-Embedding-0.6B",
        timeout=10.0,
        **kwargs,
    )
    # 替换引擎内部 httpx.Client 的 transport,拦截真实请求
    # 通过 monkeypatch _post_embeddings 所用的 Client
    orig_client = httpx.Client

    def _patched_client(*args, **kw):
        kw["transport"] = transport
        return orig_client(*args, **kw)

    httpx.Client = _patched_client  # type: ignore[assignment]
    return engine


@pytest.fixture(autouse=True)
def _restore_httpx():
    orig = httpx.Client
    yield
    httpx.Client = orig  # type: ignore[assignment]


def test_embed_returns_vector():
    calls: list[dict] = []
    engine = _make_engine(_mock_embeddings_handler(calls))
    vec = engine.embed("hello")
    assert isinstance(vec, tuple)
    assert len(vec) == 3
    # 只发一次请求,input 为单条
    assert calls[0]["inputs"] == ["hello"]
    assert calls[0]["model"] == "Qwen3-Embedding-0.6B"


def test_embed_is_cached():
    """同文本第二次 embed 不应再发请求(lru_cache 命中)。"""
    calls: list[dict] = []
    engine = _make_engine(_mock_embeddings_handler(calls))
    engine.embed("cached-text")
    engine.embed("cached-text")
    engine.embed("cached-text")
    # 仅首次命中网络
    assert len(calls) == 1


def test_similarity_identical_vectors_is_one():
    engine = _make_engine(_mock_embeddings_handler([]))
    v = [1.0, 0.0, 0.0]
    assert engine.similarity(v, v) == pytest.approx(1.0)


def test_similarity_orthogonal_is_zero():
    engine = _make_engine(_mock_embeddings_handler([]))
    assert engine.similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)


def test_similarity_empty_is_zero():
    engine = _make_engine(_mock_embeddings_handler([]))
    assert engine.similarity([], [1.0]) == 0.0


def test_embed_batch_single_request():
    """批量 embed 应在单次请求内完成。"""
    calls: list[dict] = []
    engine = _make_engine(_mock_embeddings_handler(calls))
    vecs = engine.embed_batch(["a", "bb", "ccc"])
    assert len(vecs) == 3
    assert len(calls) == 1
    assert calls[0]["inputs"] == ["a", "bb", "ccc"]


def test_missing_api_base_raises():
    with pytest.raises(ValueError, match="embed_api_base"):
        QwenEmbedding(api_base="", model="x")


def test_missing_model_raises():
    with pytest.raises(ValueError, match="embed_model"):
        QwenEmbedding(api_base="http://x/v1", model="")


def test_retry_on_5xx_then_success():
    """5xx 瞬态错误应重试,最终成功。"""
    attempt = {"n": 0}

    def flaky(request: httpx.Request) -> httpx.Response:
        attempt["n"] += 1
        if attempt["n"] == 1:
            return httpx.Response(503, text="transient")
        body = json.loads(request.content)
        return httpx.Response(
            200,
            json={"data": [{"embedding": [1.0, 0.0], "index": 0} for _ in body["input"]]},
        )

    engine = _make_engine(flaky, max_retries=2)
    vec = engine.embed("retry-me")
    assert len(vec) == 2
    assert attempt["n"] == 2  # 第一次失败,第二次成功


def test_persists_through_protocol_interface():
    """QwenEmbedding 满足 EmbeddingEngine Protocol(embed + similarity)。"""
    from document_comparison.embed.base import EmbeddingEngine

    engine = _make_engine(_mock_embeddings_handler([]))
    assert isinstance(engine, EmbeddingEngine)
