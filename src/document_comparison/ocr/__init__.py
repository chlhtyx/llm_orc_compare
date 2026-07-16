"""② OCR 文档理解层:引擎工厂按 ocr_backend 路由。

- llm(默认):通过远端多模态推理服务(OpenAI 兼容 API)识别 PDF 版面。
  适配能返回结构化 JSON 的通用对话 VL 模型(Qwen-VL-Max / Qwen3-VL / GPT-4o 等)。
  独立配置:paddleocr_api_base / paddleocr_api_key / paddleocr_model(不复用 llm_*)。
- paddleocr:走同一套 OpenAI 兼容推理服务,但用不同的输出解析逻辑。
  适配返回纯文本/Markdown 的专用 OCR 模型(PaddleOCR-VL 等),这类模型不遵循
  JSON 指令。独立配置:paddleocr_api_base / paddleocr_api_key / paddleocr_model。
"""
from __future__ import annotations

from ..config import settings
from .base import OCREngine
from .llm import LLMOCREngine
from .native import NativePDFEngine
from .paddleocr_http import PaddleOCREngine
from .trusted import TrustedPDFReader


def get_ocr_engine(backend: str | None = None) -> OCREngine:
    """按引擎名构造 OCR 引擎。

    backend 为 None 时回退到 settings.ocr_backend(代码默认 'llm',
    不再持久化、不在设置页暴露)。
    """
    backend = backend or settings.ocr_backend
    if backend == "paddleocr":
        fallback: OCREngine = PaddleOCREngine()
    else:
        fallback = LLMOCREngine()
    # 无论视觉 OCR 选哪一个 adapter，都先尝试确定性的 PDF 原生读取。
    return TrustedPDFReader(fallback)


def get_text_ocr_engine(backend: str | None = None):
    """构造「整图单次取纯文本」的底层 OCR 引擎(无标注版管线用)。

    与 get_ocr_engine 的区别:不包装 TrustedPDFReader(整篇原生读取由
    whole_doc.extract_native_full_text 负责),直接返回带 recognize_text 方法的
    LLMOCREngine / PaddleOCREngine,供长图整体 OCR 调用。
    """
    backend = backend or settings.ocr_backend
    if backend == "paddleocr":
        return PaddleOCREngine()
    return LLMOCREngine()


__all__ = [
    "OCREngine",
    "get_ocr_engine",
    "get_text_ocr_engine",
    "LLMOCREngine",
    "NativePDFEngine",
    "PaddleOCREngine",
    "TrustedPDFReader",
]
