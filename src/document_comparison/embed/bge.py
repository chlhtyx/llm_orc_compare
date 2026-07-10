"""bge-m3 向量引擎(生产用,需下载模型)。延迟导入。"""
from __future__ import annotations

from typing import Any


class BgeEmbedding:  # pragma: no cover - 需模型环境
    def __init__(self, model_name: str = "BAAI/bge-m3") -> None:
        self._model = None
        self._model_name = model_name

    def _ensure(self):
        if self._model is None:
            from FlagEmbedding import BGEM3FlagModel  # type: ignore[import-untyped]

            self._model = BGEM3FlagModel(self._model_name, use_fp16=True)
        return self._model

    def embed(self, text: str) -> Any:
        model = self._ensure()
        return model.encode([text], return_dense=True)["dense_vecs"][0].tolist()

    def similarity(self, vec_a: Any, vec_b: Any) -> float:
        import math

        dot = sum(a * b for a, b in zip(vec_a, vec_b))
        na = math.sqrt(sum(a * a for a in vec_a))
        nb = math.sqrt(sum(b * b for b in vec_b))
        return dot / (na * nb) if na and nb else 0.0
