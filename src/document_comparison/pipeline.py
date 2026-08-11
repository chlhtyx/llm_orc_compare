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
from .align.llm_resolver import (
    resolve_alignment_candidates,
    resolve_raw_alignment_plan,
)
from .align.raw_plan import align_raw_items
from .config import Settings, settings
from .embed import get_embed_engine, is_mock_engine
from .models import Block, DocType, RawItem, TamperReport, TruncationRecord
from .ocr import get_ocr_engine
from .ocr.quality import apply_recognition_gate
from .observability import record_ocr_result, timed_stage
from .parsing import (
    attach_docx_layout,
    count_pages,
    estimate_page_count,
    extract_text_blocks,
    get_page_metas,
    parse_word,
    slice_pdf,
)
from .report import build_report
from .structure import blocks_to_raw, build_clauses

logger = logging.getLogger(__name__)

ProgressCb = Callable[[str, float], None]


def _enforce_ocr_page_indexes(pages_blocks: list[list[Block]]) -> list[list[Block]]:
    """以 OCR 返回的外层页序为准，校正块中错误的页号。

    ``OCREngine.recognize`` 的返回值按页分组，外层下标才是跨引擎稳定的页归属。
    第三方 OCR/版面服务偶发把多页结果都标成同一个 ``page_index``；若直接信任
    该字段，后续条款、报告和 PDF 预览会把不同页的高亮全部叠到同一页。
    """
    corrected_pages: set[int] = set()
    corrected_blocks = 0
    normalized: list[list[Block]] = []
    for expected_page_index, blocks in enumerate(pages_blocks):
        normalized_page: list[Block] = []
        for block in blocks:
            if block.page_index != expected_page_index:
                corrected_pages.add(expected_page_index)
                corrected_blocks += 1
                block = block.model_copy(
                    update={"page_index": expected_page_index}
                )
            normalized_page.append(block)
        normalized.append(normalized_page)
    if corrected_blocks:
        logger.warning(
            "ocr block page-index mismatch corrected blocks=%s pages=%s",
            corrected_blocks,
            sorted(corrected_pages),
        )
    return normalized


def _parse_source(
    path: str | Path,
    ocr,
    cfg: Settings,
    on_progress: ProgressCb | None = None,
) -> tuple[list[RawItem], DocType]:
    """解析原始合同(source),按后缀分发。

    `.docx` → ``parse_word``(结构化,带 Word 标题层级);
    `.pdf`  → 复用 OCR 通道(原生文本层优先,失败兜底视觉 OCR)→ ``blocks_to_raw``。

    返回 (RawItem 列表, doc_type)。调用方据此 ``build_clauses(raw, doc_type)``。
    PDF source 与 target 走同一解析路径,失去 Word 标题层级,仅靠编号模式推断层级。
    """
    src_path = Path(path)
    if src_path.suffix.lower() == ".pdf":
        page_metas = get_page_metas(src_path, cfg.pdf_render_dpi)
        pages_blocks = ocr.recognize(src_path, page_metas, on_progress=on_progress)
        pages_blocks = _enforce_ocr_page_indexes(pages_blocks)
        logger.info(
            "source pdf parsed via ocr pages=%s blocks=%s",
            len(page_metas), sum(len(b) for b in pages_blocks),
        )
        return blocks_to_raw(pages_blocks), "pdf"
    # 默认 .docx(API 层已保证后缀合法性)
    return parse_word(src_path), "word"


def _source_visual_layout(
    source_path: str | Path,
    source_doc_type: DocType,
    source_annotation_pdf_path: str | Path | None,
    ocr,
    cfg: Settings,
) -> tuple[list, list[list[Block]]]:
    """读取原件侧可视化 PDF 的页信息和真实文本块。

    PDF 原件直接使用上传文件；DOCX 仅接受任务层已经由 LibreOffice 生成的派生
    PDF。优先取 PDF 原生文本层，只有文本层为空才走 OCR，避免把原件标注变成额外
    的远端模型调用。无坐标/歧义由后续映射留空，不根据 Word 页序补造位置。
    """
    visual_path: Path | None
    if source_doc_type == "pdf":
        visual_path = Path(source_path)
    elif source_annotation_pdf_path is not None:
        candidate = Path(source_annotation_pdf_path)
        visual_path = candidate if candidate.is_file() else None
    else:
        visual_path = None
    if visual_path is None:
        return [], []
    try:
        page_metas = get_page_metas(visual_path, cfg.pdf_render_dpi)
        pages_blocks = extract_text_blocks(visual_path)
        if not any(pages_blocks):
            pages_blocks = ocr.recognize(visual_path, page_metas, on_progress=None)
            pages_blocks = _enforce_ocr_page_indexes(pages_blocks)
            logger.info("source visual PDF used OCR fallback pages=%s", len(page_metas))
        else:
            logger.info("source visual PDF used native text blocks pages=%s", len(page_metas))
        return page_metas, pages_blocks
    except Exception as exc:  # noqa: BLE001
        logger.warning("source visual PDF layout unavailable: %s", exc)
        return [], []


def _apply_docx_annotation_state(report: TamperReport, source_doc_type: DocType) -> None:
    """为 DOCX 派生 PDF 写入可视化标注状态，而不影响比对结论。"""
    if source_doc_type != "word":
        return
    if not report.source_page_meta:
        report.source_annotation_status = "unavailable"
        report.source_annotation_reason = "原始 DOCX 未能渲染为可用 PDF，无法生成版面标注"
        return
    source_side_diffs = [
        diff for diff in [*report.diffs, *report.unmatched_clauses]
        if diff.status != "added"
    ]
    missing = [diff for diff in source_side_diffs if not diff.source_page_regions]
    if missing:
        report.source_annotation_status = "partial"
        report.source_annotation_reason = (
            "原始 DOCX 已渲染为 PDF；部分差异未能在渲染版面中唯一定位，未标注"
        )
    else:
        report.source_annotation_status = "available"
        report.source_annotation_reason = ""


def run_pipeline(
    word_path: str | Path,
    pdf_path: str | Path,
    cfg: Settings | None = None,
    ocr=None,
    embed=None,
    on_progress: ProgressCb | None = None,
    enable_llm_judge: bool = False,
    enable_llm_alignment: bool = False,
    ocr_backend: str | None = None,
    enable_risk_assessment: bool = False,
    enable_llm_direct_diff: bool = False,
    truncate_to_original_pages: bool = False,
    original_page_count: int | None = None,
    truncated_pdf_output_path: str | Path | None = None,
    source_annotation_pdf_path: str | Path | None = None,
) -> TamperReport:
    cfg = cfg or settings
    # 标准管线需要 PDF 标注：PaddleOCR 内容无坐标时追加 Spotting 调用。
    # 对帐单等不需要标注的通道仍使用 get_ocr_engine 默认单次 OCR。
    ocr = ocr or get_ocr_engine(ocr_backend, enable_spotting=True)
    embed = embed or get_embed_engine(cfg.embed_backend)
    thresholds = {"identical": cfg.similarity_identical, "modified": cfg.similarity_modified}

    # 互斥兜底:LLM 直接比对分支会早返回,标准管线的对齐/辅助说明不会执行。
    # 这里静默归一,避免上层(UI/外部 API)同时传 true 时产生"对齐/辅助说明也在跑"的误解。
    # 风险评估不归一:它在 direct_diff 分支内仍作用于 apply_recognition_gate(verdict 镜像)。
    if enable_llm_direct_diff and (enable_llm_alignment or enable_llm_judge):
        logger.warning(
            "enable_llm_direct_diff=True 隐式忽略 enable_llm_alignment/enable_llm_judge"
            "(direct=%s alignment=%s judge=%s) —— 直接比对分支不进入标准管线",
            enable_llm_direct_diff, enable_llm_alignment, enable_llm_judge,
        )
        enable_llm_alignment = False
        enable_llm_judge = False

    def _progress(stage: str, frac: float) -> None:
        if on_progress:
            on_progress(stage, frac)

    # —— 回收件页数截取(可选;所有比对模式共享)——
    # 物理截断保证 OCR/报告/高亮图在页数维度一致;构造 TruncationRecord 留痕。
    pdf_path = Path(pdf_path)
    truncation: TruncationRecord | None = None
    if truncate_to_original_pages:
        if original_page_count is not None and original_page_count <= 0:
            raise ValueError("original_page_count 必须 >= 1")
        doc_source = "explicit" if original_page_count is not None else "estimated"
        orig_pages = (
            original_page_count
            if original_page_count is not None
            # .docx 用 OOXML 估算;PDF source 直接读页数(PDF 无需估算)。
            else (
                count_pages(word_path)
                if Path(word_path).suffix.lower() == ".pdf"
                else estimate_page_count(word_path)
            )
        )
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
            pdf_path = slice_pdf(
                pdf_path,
                orig_pages,
                output_path=truncated_pdf_output_path,
            )
            truncation = TruncationRecord(
                original_pdf_page_count=pdf_pages,
                truncated_pdf_page_count=orig_pages,
                original_doc_page_count=orig_pages,
                doc_page_count_source=doc_source,
            )
            _progress("pdf_truncated", 0.09)
        else:
            logger.info(
                "truncate option on but no truncation needed: pdf=%s original=%s",
                pdf_pages, orig_pages,
            )

    # —— LLM 直接比对分支(可选)——
    # 开启后跳过条款切分/对齐/裁决,把 Word 与 PDF 各自解析成纯文本后,
    # 直接交给 LLM 比对差异并标注(复用无标注版管线 + llm_text_diff)。
    # 结果适配为标准 TamperReport,使任务/历史/外部 API/报告页/JSON-PDF-DOCX
    # 全链路无感复用;LLM 仅做语义差异,不做风险分级(per-diff risk_level=none)。
    if enable_llm_direct_diff:
        from .compare.llm_diff import llm_text_diff, text_diff_to_tamper_report
        from .report.builder import locate_hunk_regions
        from .structure.normalize import normalize_text

        logger.info(
            "llm direct diff branch: word=%s pdf=%s ocr_backend=%s",
            word_path, pdf_path, ocr_backend,
        )

        # —— source → 纯文本(.docx 解析展平;.pdf 走 OCR blocks 展平)——
        _progress("word_parsing", 0.02)
        with timed_stage(logger, "raw_word_parse"):
            word_raw, _word_doc_type = _parse_source(word_path, ocr, cfg, on_progress=None)
            word_text = "\n".join(item.text for item in word_raw if item.text)
        _progress("word_done", 0.10)

        # —— PDF → 结构化 blocks(同时作为「文本来源」与「坐标来源」)——
        # 关键:文本与定位 block 必须同源。若 pdf_text 来自 whole_doc(原生/纯文本 OCR)
        # 而坐标 block 来自结构化 SDK 解析,两端断句/表格表示不一致,LLM 摘出的片段
        # 到 block content 里做子串匹配基本落空 → 高亮全缺。这里直接用结构化 OCR 的
        # blocks 拼 pdf_text,使 LLM 看到的文本就是 block content 的拼接,定位可靠命中。
        _progress("ocr", 0.15)
        with timed_stage(logger, "llm_direct_ocr"):
            direct_page_metas = get_page_metas(pdf_path, cfg.pdf_render_dpi)
            pages_blocks = ocr.recognize(
                Path(pdf_path), direct_page_metas, on_progress=_progress
            )
            pages_blocks = _enforce_ocr_page_indexes(pages_blocks)
        total_blocks = sum(len(b) for b in pages_blocks)
        with_bbox = sum(
            1 for page in pages_blocks for b in page if len(b.bbox) >= 4
        )
        logger.info(
            "llm direct diff ocr pages=%s blocks=%s with_bbox=%s",
            len(direct_page_metas), total_blocks, with_bbox,
        )
        _progress("ocr_done", 0.70)

        # —— blocks → pdf_text(按页序拼接 block content)——
        pdf_text = "\n".join(
            b.content for page in pages_blocks for b in page if b.content
        )
        diagnostics = list(getattr(ocr, "last_diagnostics", []) or [])
        record_ocr_result(
            logger,
            pages_blocks,
            diagnostics=diagnostics,
            stage="llm-direct",
            metadata={"ocr_backend": ocr_backend or cfg.ocr_backend},
        )

        # —— normalize 两端 ——
        _progress("normalize", 0.75)
        word_text = normalize_text(word_text)
        pdf_text = normalize_text(pdf_text)

        # —— LLM 直接比对(单次调用)——
        _progress("diff", 0.80)
        with timed_stage(logger, "raw_llm_diff"):
            raw_report = llm_text_diff(
                word_text,
                pdf_text,
                char_level=True,
                source=str(word_path),
                target=str(pdf_path),
            )
            raw_report.recognition_diagnostics = diagnostics
        logger.info(
            "llm direct diff hunks=%s similarity=%s",
            len(raw_report.hunks), raw_report.stats.get("similarity"),
        )

        # —— 定位(同源 blocks,匹配可靠):hunk 文本 → block bbox ——
        # pmeta 直接用上面已取的 direct_page_metas;locate 内部失败/无坐标时降级为空。
        pmeta = {m.page_index: m for m in direct_page_metas}
        hunk_regions: list[list] = []
        try:
            with timed_stage(logger, "llm_direct_locate"):
                hunk_regions = locate_hunk_regions(
                    raw_report.hunks, pages_blocks, pmeta
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "llm direct diff locate failed, report without highlights: %s", exc
            )
        located = sum(1 for r in hunk_regions if r)
        logger.info(
            "llm direct diff located %s/%s hunks (bbox blocks=%s)",
            located, len(raw_report.hunks), with_bbox,
        )
        source_page_metas, source_pages_blocks = _source_visual_layout(
            word_path, _word_doc_type, source_annotation_pdf_path, ocr, cfg
        )
        source_hunk_regions: list[list] = []
        if source_page_metas:
            try:
                with timed_stage(logger, "llm_direct_source_locate"):
                    source_hunk_regions = locate_hunk_regions(
                        raw_report.hunks,
                        source_pages_blocks,
                        {m.page_index: m for m in source_page_metas},
                        side="source",
                        require_unique_page=_word_doc_type == "word",
                    )
            except Exception as exc:  # noqa: BLE001
                logger.warning("llm direct diff source locate failed: %s", exc)

        _progress("compare", 0.90)
        with timed_stage(logger, "compare_and_report"):
            report = text_diff_to_tamper_report(
                raw_report,
                source=str(word_path),
                target=str(pdf_path),
                truncation=truncation,
                page_regions=hunk_regions,
                page_metas=direct_page_metas,
                source_page_regions=source_hunk_regions,
                source_page_metas=source_page_metas,
            )
            apply_recognition_gate(
                report,
                diagnostics,
                enable_risk_assessment=enable_risk_assessment,
            )
            _apply_docx_annotation_state(report, _word_doc_type)
        _progress("compare_done", 1.0)
        logger.info(
            "llm direct diff report diffs=%s similarity=%s status=%s located=%s",
            len(report.diffs),
            report.summary.get("similarity"),
            report.change_status,
            located,
        )
        return report

    # —— ① Word/PDF 解析(source)+ ③ 切分 ——
    _progress("word_parsing", 0.02)
    with timed_stage(logger, "word_parse_and_structure"):
        word_raw, word_doc_type = _parse_source(word_path, ocr, cfg, on_progress=None)
        source_page_metas, source_pages_blocks = _source_visual_layout(
            word_path, word_doc_type, source_annotation_pdf_path, ocr, cfg
        )
        if word_doc_type == "word" and source_pages_blocks:
            word_raw, _mapped_items = attach_docx_layout(word_raw, source_pages_blocks)
        # RawSpan 联合对齐成功时不应依赖 Clause 切分；仅在关闭或失败回退时构建。
        word_clauses = (
            None if enable_llm_alignment else build_clauses(word_raw, word_doc_type)
        )
    logger.info(
        "word structured items=%s clauses=%s",
        len(word_raw),
        "deferred" if word_clauses is None else len(word_clauses),
    )
    _progress("word_done", 0.08)

    # —— ① PDF + ② OCR + ③ 切分 ——
    _progress("ocr", 0.10)
    with timed_stage(logger, "pdf_metadata"):
        page_metas = get_page_metas(pdf_path, cfg.pdf_render_dpi)
    with timed_stage(logger, "pdf_ocr", pages=len(page_metas)):
        pages_blocks = ocr.recognize(Path(pdf_path), page_metas, on_progress=_progress)
        pages_blocks = _enforce_ocr_page_indexes(pages_blocks)
    diagnostics = list(getattr(ocr, "last_diagnostics", []) or [])
    record_ocr_result(
        logger,
        pages_blocks,
        diagnostics=diagnostics,
        stage="compare",
        metadata={"ocr_backend": ocr_backend or cfg.ocr_backend},
    )
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
        pdf_clauses = (
            None if enable_llm_alignment else build_clauses(pdf_raw, "pdf")
        )
    logger.info(
        "pdf structured clauses=%s",
        "deferred" if pdf_clauses is None else len(pdf_clauses),
    )
    _progress("structure_done", 0.78)

    # —— ④ 对齐 ——
    # 阈值自适应:mock 字符袋相似度系统性偏低,用较低阈值;语义后端用标准阈值。
    align_threshold = cfg.align_similarity_mock if is_mock_engine(embed) else cfg.align_similarity
    _progress("align", 0.80)
    with timed_stage(logger, "clause_alignment"):
        raw_alignment = (
            align_raw_items(
                word_raw,
                pdf_raw,
                embed=embed,
                planner=resolve_raw_alignment_plan,
            )
            if enable_llm_alignment
            else None
        )
        if raw_alignment is not None:
            word_by = raw_alignment.word_by
            pdf_by = raw_alignment.pdf_by
            alignments = raw_alignment.alignments
            word_clauses = list(word_by.values())
            pdf_clauses = list(pdf_by.values())
            logger.info(
                "raw-span alignment plan accepted groups=%s",
                len(alignments),
            )
        else:
            if word_clauses is None:
                word_clauses = build_clauses(word_raw, word_doc_type)
            if pdf_clauses is None:
                pdf_clauses = build_clauses(pdf_raw, "pdf")
            alignments = align_clauses(
                word_clauses,
                pdf_clauses,
                embed,
                align_threshold,
                llm_resolver=(
                    resolve_alignment_candidates if enable_llm_alignment else None
                ),
            )
            word_by = {c.clause_id: c for c in word_clauses}
            pdf_by = {c.clause_id: c for c in pdf_clauses}
            if enable_llm_alignment:
                logger.info("raw-span alignment unavailable, used clause fallback")
    logger.info(
        "aligned word=%s pdf=%s total=%s threshold=%.2f",
        len(word_clauses), len(pdf_clauses), len(alignments), align_threshold,
    )
    _progress("align_done", 0.88)

    # —— ⑤⑥ 比对 + 报告 ——
    _progress("compare", 0.90)
    with timed_stage(logger, "compare_and_report"):
        report = build_report(
            alignments=alignments,
            word_by=word_by,
            pdf_by=pdf_by,
            embed=embed,
            page_metas=page_metas,
            source_page_metas=source_page_metas,
            thresholds=thresholds,
            source=str(word_path),
            target=str(pdf_path),
            enable_llm_judge=enable_llm_judge,
            enable_risk_assessment=enable_risk_assessment,
            truncation=truncation,
        )
        apply_recognition_gate(
            report, diagnostics, enable_risk_assessment=enable_risk_assessment,
        )
        _apply_docx_annotation_state(report, word_doc_type)
    _progress("compare_done", 1.0)

    return report
