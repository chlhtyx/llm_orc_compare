"""② OCR 文档理解层:多模态 LLM 引擎。

通过远端多模态推理服务(OpenAI 兼容 API)识别 PDF 版面。
所有多模态模型(PaddleOCR-VL / 通义千问 VL / GPT-4o 等)统一走
OpenAI 兼容 API,共用同一套配置(api_base / api_key / model)。
"""
from __future__ import annotations

from .base import OCREngine
from .llm import LLMOCREngine


def get_ocr_engine():
    return LLMOCREngine()


__all__ = ["OCREngine", "get_ocr_engine", "LLMOCREngine"]
