"""② OCR 文档理解层:引擎工厂按 ocr_backend 路由。

- llm(默认):通过远端多模态推理服务(OpenAI 兼容 API)识别 PDF 版面。
  适配能返回结构化 JSON 的通用对话 VL 模型(Qwen-VL-Max / Qwen3-VL / GPT-4o 等)。
  独立配置:llm_api_base / llm_api_key / llm_model。
- paddleocr:可切换原有 vLLM Chat Completions 与 PaddleOCR 官方 Python SDK。
  原 vLLM 配置与 paddleocr_official_* 官方 SDK 配置分开保存。
"""
from __future__ import annotations

from ..config import ensure_llm_config_fresh, settings
from .base import OCREngine
from .llm import LLMOCREngine
from .native import NativePDFEngine
from .paddleocr_http import PaddleOCREngine
from .trusted import TrustedPDFReader


def get_ocr_engine(
    backend: str | None = None, *, enable_spotting: bool = False
) -> OCREngine:
    """按引擎名构造 OCR 引擎。

    backend 为 None 时回退到 settings.ocr_backend(代码默认 'llm',
    不再持久化、不在设置页暴露)。enable_spotting 仅供需要 PDF 高亮的
    标准合同比对使用；其它通道保持单次 OCR 调用。
    """
    backend = backend or settings.ocr_backend
    # 多 worker 同步:确保本进程 settings 是最新(其它 worker 可能改过配置)。
    # 廉价版本检查,落后才 apply;异常吞掉不影响引擎构造。
    ensure_llm_config_fresh()
    if backend == "paddleocr":
        fallback: OCREngine = PaddleOCREngine(enable_spotting=enable_spotting)
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
    # 多 worker 同步:确保本进程 settings 是最新(其它 worker 可能改过配置)。
    # 廉价版本检查,落后才 apply;异常吞掉不影响引擎构造。
    ensure_llm_config_fresh()
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
