"""无标注版比对流水线(独立逻辑,与标注版 TamperReport 体系完全隔离)。

流程:
  ① Word → 纯文本(parse_word 展平)
  ② PDF → 纯文本:
       - 整篇文字层累计判定;带可靠文字层 → 直接用原生文本
       - 扫描件 → 逐页渲染 PNG 并发 OCR,按页序拼接成整篇纯文本
  ③ 双端 normalize_text
  ④ LLM 直接比对两段纯文本 → 结构化 TextDiffReport

与标注版管线(pipeline.py)不共享任何中间结构,不经过条款对齐/风险分级。
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable

from .compare.llm_diff import llm_text_diff
from .config import Settings, settings
from .models import TextDiffReport
from .ocr import get_text_ocr_engine
from .observability import record_ocr_text_result, timed_stage
from .parsing import parse_word
from .structure.normalize import normalize_text

logger = logging.getLogger(__name__)

ProgressCb = Callable[[str, float], None]


def run_raw_pipeline(
    word_path: str | Path,
    pdf_path: str | Path,
    *,
    cfg: Settings | None = None,
    ocr=None,
    on_progress: ProgressCb | None = None,
    char_level: bool = False,
    ocr_backend: str | None = None,
) -> TextDiffReport:
    """无标注版管线:Word→文本、PDF→文本(原生优先/长图整体 OCR)、LLM 比对。

    `ocr` 参数保留兼容:传入则用作整图 OCR 引擎(需有 recognize_text 方法),
    否则按 ocr_backend 构造底层引擎。
    """
    cfg = cfg or settings
    text_ocr = ocr if ocr is not None else get_text_ocr_engine(ocr_backend)

    def _progress(stage: str, frac: float) -> None:
        if on_progress:
            on_progress(stage, frac)

    # —— ① Word → 纯文本 ——
    _progress("word_parsing", 0.02)
    with timed_stage(logger, "raw_word_parse"):
        word_raw = parse_word(word_path)
        word_text = "\n".join(item.text for item in word_raw if item.text)
    logger.info("word flattened items=%s chars=%s", len(word_raw), len(word_text))
    _progress("word_done", 0.10)

    # —— ② PDF → 纯文本(整篇原生优先,扫描件长图整体 OCR)——
    _progress("ocr", 0.15)
    from .ocr.whole_doc import extract_native_full_text, ocr_whole_document

    with timed_stage(logger, "raw_pdf_native_probe"):
        native_read = extract_native_full_text(pdf_path)
    if native_read.reliable:
        pdf_text = native_read.text
        diagnostic = native_read.diagnostic
        logger.info("pdf native full text chars=%s", len(pdf_text))
    else:
        logger.info("pdf native unreliable(%s), fallback to whole-doc ocr (per-page concurrent)", native_read.diagnostic.reasons)
        read = ocr_whole_document(pdf_path, text_ocr, on_progress=_progress)
        pdf_text = read.text
        diagnostic = read.diagnostic
    record_ocr_text_result(
        logger,
        pdf_text,
        diagnostic=diagnostic,
        stage="raw",
        metadata={"ocr_backend": ocr_backend or cfg.ocr_backend},
    )
    _progress("ocr_done", 0.70)

    # —— ③ normalize ——
    _progress("normalize", 0.75)
    with timed_stage(logger, "raw_normalize"):
        word_text = normalize_text(word_text)
        pdf_text = normalize_text(pdf_text)

    # —— ④ LLM 差异比对 ——
    _progress("diff", 0.80)
    with timed_stage(logger, "raw_llm_diff"):
        report = llm_text_diff(
            word_text,
            pdf_text,
            char_level=char_level,
            source=str(word_path),
            target=str(pdf_path),
        )
        report.recognition_diagnostics = [diagnostic]
        if not diagnostic.reliable:
            report.recognition_status = "needs_review"
            report.stats["recognition_quality"] = "needs_review"
            report.stats["unreliable_pages"] = [diagnostic.page_index + 1]
    logger.info(
        "llm diff report hunks=%s similarity=%s reliable=%s",
        len(report.hunks), report.stats.get("similarity"), diagnostic.reliable,
    )
    _progress("done", 1.0)
    return report
