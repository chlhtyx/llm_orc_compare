"""② OCR 文档理解层:引擎工厂按 ocr_backend 路由。

- llm(默认):通过远端多模态推理服务(OpenAI 兼容 API)识别 PDF 版面。
  适配能返回结构化 JSON 的通用对话 VL 模型(Qwen-VL-Max / Qwen3-VL / GPT-4o 等)。
  共用配置:llm_api_base / llm_api_key / llm_model。
- paddleocr:走同一套 OpenAI 兼容推理服务,但用不同的输出解析逻辑。
  适配返回纯文本/Markdown 的专用 OCR 模型(PaddleOCR-VL 等),这类模型不遵循
  JSON 指令。共用配置:llm_api_base / llm_api_key / llm_model(切换引擎无需改连接)。
"""
from __future__ import annotations

from ..config import settings
from .base import OCREngine
from .llm import LLMOCREngine
from .paddleocr_http import PaddleOCREngine


def get_ocr_engine() -> OCREngine:
    backend = settings.ocr_backend
    if backend == "paddleocr":
        return PaddleOCREngine()
    # 默认 llm
    return LLMOCREngine()


__all__ = ["OCREngine", "get_ocr_engine", "LLMOCREngine", "PaddleOCREngine"]
