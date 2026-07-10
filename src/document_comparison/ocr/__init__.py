"""② OCR 文档理解层:可替换引擎。

- `mock`  PyMuPDF 文本层,适合开发与文本 PDF(默认)
- `vllm`  远端多模态推理服务(OpenAI 兼容 API)

所有多模态模型(PaddleOCR-VL / 通义千问 VL / GPT-4o 等)统一走
OpenAI 兼容 API,共用同一套配置(api_base / api_key / model),
按当前 model 路由到对应服务。
引擎选择由 DC_OCR_BACKEND 控制(见 config)。
"""
from __future__ import annotations

from ..config import settings
from .base import OCREngine


def get_ocr_engine(backend: str | None = None):
    backend = (backend or settings.ocr_backend).lower()
    if backend in ("llm", "vllm", "paddle"):  # paddle 保留为静默别名(向后兼容)
        from .llm import LLMOCREngine

        return LLMOCREngine()
    if backend != "mock":
        raise ValueError(f"未知 OCR backend: {backend}(支持 mock / vllm / llm)")
    from .textlayer import TextLayerOCR

    return TextLayerOCR()


__all__ = ["OCREngine", "get_ocr_engine"]
