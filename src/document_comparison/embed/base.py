"""向量引擎接口约定。"""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class EmbeddingEngine(Protocol):
    """文本向量化与相似度计算。

    embed() 返回不透明向量对象(mock 用 Counter,真实用 list[float]),
    对齐层只依赖 similarity() 的标量结果。
    """

    def embed(self, text: str) -> Any: ...

    def similarity(self, vec_a: Any, vec_b: Any) -> float: ...
