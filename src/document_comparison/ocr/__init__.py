"""② OCR 文档理解层:可替换引擎。

- `mock`  PyMuPDF 文本层,适合开发与文本 PDF(默认)
- `vllm`  远端多模态推理服务(OpenAI 兼容 API)
- `paddle` / `llm`  `vllm` 的别名(向后兼容)

PaddleOCR-VL 与通义千问 VL 等均通过 vLLM 部署,共用同一套 API 配置
(api_base / api_key / model),按当前 model 路由到对应服务。
引擎选择由 DC_OCR_BACKEND 控制(见 config)。
"""
from __future__ import annotations

from ..config import settings
from .base import OCREngine


def get_ocr_engine(backend: str | None = None) -> OCREngine:
    backend = (backend or settings.ocr_backend).lower()
    if backend in ("paddle", "llm", "vllm"):
        from .llm import LLMOCREngine

        return LLMOCREngine()
    if backend != "mock":
        raise ValueError(f"未知 OCR backend: {backend}(支持 mock / vllm)")
    from .textlayer import TextLayerOCR

    return TextLayerOCR()


__all__ = ["OCREngine", "get_ocr_engine"]
