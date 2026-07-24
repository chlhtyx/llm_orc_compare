"""比对流水线编排:① → ⑥ 全链路(§3)。

run_pipeline(word_path, pdf_path) 跑通:
Word 解析 → 条款切分;PDF 渲染 → OCR → 条款切分;对齐;比对;报告。
支持可选的 on_progress 回调,按阶段推送实时进度(stage, 0-1)。
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable

from .align import align_clauses
from .config import Settings, settings
from .embed import get_embed_engine, is_mock_engine
from .models import TamperReport, TruncationRecord
from .ocr import get_ocr_engine
from .ocr.quality import apply_recognition_gate
from .observability import timed_stage
from .parsing import count_pages, estimate_page_count, get_page_metas, parse_word, slice_pdf
from .report import build_report
from .structure import blocks_to_raw, build_clauses

logger = logging.getLogger(__name__)

ProgressCb = Callable[[str, float], None]


def run_pipeline(
    word_path: str | Path,
    pdf_path: str | Path,
    cfg: Settings | None = None,
    ocr=None,
    embed=None,
    on_progress: ProgressCb | None = None,
    enable_llm_judge: bool = False,
    ocr_backend: str | None = None,
    enable_risk_assessment: bool = False,
    truncate_to_original_pages: bool = False,
    original_page_count: int | None = None,
) -> TamperReport:
    cfg = cfg or settings
    # 标准管线需要 PDF 标注：PaddleOCR 内容无坐标时追加 Spotting 调用。
    # 对帐单等不需要标注的通道仍使用 get_ocr_engine 默认单次 OCR。
    ocr = ocr or get_ocr_engine(ocr_backend, enable_spotting=True)
    embed = embed or get_embed_engine(cfg.embed_backend)
    thresholds = {"identical": cfg.similarity_identical, "modified": cfg.similarity_modified}

    def _progress(stage: str, frac: float) -> None:
        if on_progress:
            on_progress(stage, frac)

    # —— ① Word 解析 + ③ 切分 ——
    _progress("word_parsing", 0.02)
    with timed_stage(logger, "word_parse_and_structure"):
        word_raw = parse_word(word_path)
        word_clauses = build_clauses(word_raw, "word")
    logger.info("word structured items=%s clauses=%s", len(word_raw), len(word_clauses))
    _progress("word_done", 0.08)

    # —— 回收件页数截取(可选)——
    # 回收 PDF 页数超过原始合同时,物理截取到原始页数。物理截断而非逻辑截断:
    # 下游 burn_pdf/高亮图按 len(doc) 逐页遍历并索引 page_meta,截断后文件页数
    # 与 page_metas/page_meta 自动一致,避免越界。
    # 截断发生时构造 TruncationRecord 写入报告(等保审计留痕),并推送里程碑事件。
    pdf_path = Path(pdf_path)
    truncation: TruncationRecord | None = None
    if truncate_to_original_pages:
        # original_page_count 显式传入优先;否则按 docx OOXML 估算(会偏少)。
        doc_source = "explicit" if original_page_count else "estimated"
        orig_pages = original_page_count or estimate_page_count(word_path)
        try:
            pdf_pages = count_pages(pdf_path)
        except Exception as exc:  # noqa: BLE001
            logger.warning("count pdf pages failed, skip truncation: %s", exc)
            pdf_pages = 0
        if pdf_pages > orig_pages:
            logger.info(
                "truncate recovered pdf to original pages: pdf=%s original=%s -> slice",
                pdf_pages, orig_pages,
            )
            pdf_path = slice_pdf(pdf_path, orig_pages)
            truncation = TruncationRecord(
                original_pdf_page_count=pdf_pages,
                truncated_pdf_page_count=orig_pages,
                original_doc_page_count=orig_pages,
                doc_page_count_source=doc_source,
            )
            # 截断是影响比对范围的关键操作,经 SSE + task_events 留痕(需白名单放行)
            _progress("pdf_truncated", 0.09)
        else:
            logger.info(
                "truncate option on but no truncation needed: pdf=%s original=%s",
                pdf_pages, orig_pages,
            )

    # —— ① PDF + ② OCR + ③ 切分 ——
    _progress("ocr", 0.10)
    with timed_stage(logger, "pdf_metadata"):
        page_metas = get_page_metas(pdf_path, cfg.pdf_render_dpi)
    with timed_stage(logger, "pdf_ocr", pages=len(page_metas)):
        pages_blocks = ocr.recognize(Path(pdf_path), page_metas, on_progress=_progress)
    total_blocks = sum(len(b) for b in pages_blocks)
    logger.info(
        "ocr done pages=%s blocks=%s dpi=%s",
        len(page_metas), total_blocks, cfg.pdf_render_dpi,
    )
    if len(page_metas) > 0 and total_blocks == 0:
        logger.warning(
            "ocr returned 0 blocks for %s pages — 模型可能未遵循 JSON 格式指令,"
            "或返回了非 blocks 结构(如专用 OCR 模型 PaddleOCR-VL / DeepSeek-OCR)。"
            "请确认 llm_model 是多模态对话模型(如 Qwen3-VL-32B-Instruct),"
            "而非专用 OCR 模型。当前 ocr_backend=%s llm_model=%s",
            len(page_metas), ocr_backend or cfg.ocr_backend, cfg.llm_model,
        )
    _progress("ocr_done", 0.70)
    _progress("structure", 0.72)
    with timed_stage(logger, "pdf_structure"):
        pdf_raw = blocks_to_raw(pages_blocks)
        pdf_clauses = build_clauses(pdf_raw, "pdf")
    logger.info("pdf structured clauses=%s", len(pdf_clauses))
    _progress("structure_done", 0.78)

    # —— ④ 对齐 ——
    # 阈值自适应:mock 字符袋相似度系统性偏低,用较低阈值;语义后端用标准阈值。
    align_threshold = cfg.align_similarity_mock if is_mock_engine(embed) else cfg.align_similarity
    _progress("align", 0.80)
    with timed_stage(logger, "clause_alignment"):
        alignments = align_clauses(
            word_clauses, pdf_clauses, embed, align_threshold
        )
    logger.info(
        "aligned word=%s pdf=%s total=%s threshold=%.2f",
        len(word_clauses), len(pdf_clauses), len(alignments), align_threshold,
    )
    _progress("align_done", 0.88)

    # —— ⑤⑥ 比对 + 报告 ——
    word_by = {c.clause_id: c for c in word_clauses}
    pdf_by = {c.clause_id: c for c in pdf_clauses}
    _progress("compare", 0.90)
    with timed_stage(logger, "compare_and_report"):
        report = build_report(
            alignments=alignments,
            word_by=word_by,
            pdf_by=pdf_by,
            embed=embed,
            page_metas=page_metas,
            thresholds=thresholds,
            source=str(word_path),
            target=str(pdf_path),
            enable_llm_judge=enable_llm_judge,
            enable_risk_assessment=enable_risk_assessment,
            truncation=truncation,
        )
        diagnostics = list(getattr(ocr, "last_diagnostics", []))
        apply_recognition_gate(
            report, diagnostics, enable_risk_assessment=enable_risk_assessment,
        )
    _progress("compare_done", 1.0)

    return report
