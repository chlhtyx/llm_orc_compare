"""Mock 向量引擎:字符袋(2-gram)余弦相似度。

不依赖任何模型,相似度反映字面重叠程度,足以驱动对齐与比对逻辑的测试。
"""
from __future__ import annotations

import math
import re
from collections import Counter
from typing import Any

_TOKEN_RE = re.compile(r"[一-鿿]|[A-Za-z0-9]+")


def _tokens(text: str) -> list[str]:
    toks: list[str] = []
    for m in _TOKEN_RE.findall(text):
        if re.fullmatch(r"[A-Za-z0-9]+", m):
            toks.append(m.lower())
        else:
            toks.append(m)
    # 字符 2-gram(中文)与词级(英文/数字)混合
    grams: list[str] = []
    for i in range(len(toks) - 1):
        grams.append(toks[i] + toks[i + 1])
    grams.extend(toks)
    return grams or toks


class MockEmbedding:
    def embed(self, text: str) -> Any:
        return Counter(_tokens(text))

    def similarity(self, vec_a: Any, vec_b: Any) -> float:
        if not vec_a or not vec_b:
            return 0.0
        keys = set(vec_a) | set(vec_b)
        dot = sum(vec_a.get(k, 0) * vec_b.get(k, 0) for k in keys)
        na = math.sqrt(sum(v * v for v in vec_a.values()))
        nb = math.sqrt(sum(v * v for v in vec_b.values()))
        if na == 0 or nb == 0:
            return 0.0
        return dot / (na * nb)
