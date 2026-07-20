"""对帐单金额统计流水线(独立逻辑,与 TamperReport / TextDiffReport 体系完全隔离)。

流程(多文件串行):
  对每个 PDF:
    ① get_ocr_engine → TrustedPDFReader(原生优先 + 视觉 OCR 逐页降级)
    ② 抽取所有 label="table" 且 table is not None 的 Block
    ③ merge_cross_page_tables 合并跨页续表
    ④ 对每张表 summarize_table:
       - 启发式 detect_amount_columns 命中 → 代码求和
       - 启发式失败 + enable_llm_column_detection → 渲染该页 PNG 调
         llm_detect_amount_columns 兜底指认列,再代码求和
       - LLM 也失败 → column_source="none",标 needs_review
  多文件聚合 → grand_total / grand_totals_by_column / verdict / reasons

与现有 raw_pipeline 的关键区别:
  - 不需要 Word 输入(单端:只读 PDF)
  - 不做 LLM 整篇比对,只做表格抽取 + 代码求和
  - LLM 调用仅用于列定位兜底(单次调用、范围有限),绝不做算术
  - 支持一次提交多个 PDF,串行处理并聚合总金额
"""
from __future__ import annotations

import logging
from decimal import Decimal
from pathlib import Path
from typing import Callable

from .config import Settings, settings
from .models import (
    Block,
    StatementFileSummary,
    StatementSummaryReport,
    StatementTableSummary,
    TableStructure,
)
from .ocr import get_ocr_engine
from .parsing.pdf import get_page_metas, render_pages
from .statement.amount_column import (
    detect_amount_columns,
    merge_cross_page_tables,
    summarize_table,
)

logger = logging.getLogger(__name__)

ProgressCb = Callable[[str, float], None]


def run_statement_pipeline(
    pdf_paths: list[str | Path],
    file_names: list[str],
    *,
    cfg: Settings | None = None,
    on_progress: ProgressCb | None = None,
    ocr_backend: str | None = None,
    amount_column_keywords: list[str] | None = None,
    enable_llm_column_detection: bool = True,
) -> StatementSummaryReport:
    """多文件串行处理,聚合输出总金额。

    单文件失败不阻断其他文件:失败记入 StatementFileSummary.error,
    且整体 verdict 标 needs_review。
    """
    cfg = cfg or settings
    total_files = len(pdf_paths)
    if total_files == 0:
        return StatementSummaryReport()

    def _progress(stage: str, frac: float) -> None:
        if on_progress:
            on_progress(stage, frac)

    file_summaries: list[StatementFileSummary] = []
    _progress("statement_start", 0.02)

    # 单文件进度区间:[0.02, 0.98],均分给每个文件,避免回到 0
    file_span_start = 0.02
    file_span_end = 0.98
    file_span_total = file_span_end - file_span_start

    for file_index, (pdf_path, file_name) in enumerate(zip(pdf_paths, file_names)):
        base = file_span_start + (file_index / total_files) * file_span_total
        span = file_span_total / total_files

        def file_progress(stage: str, frac: float, _base=base, _span=span) -> None:
            _progress(stage, _base + frac * _span * 0.95)

        _progress(f"statement_file_{file_index}", base)
        file_summary = _process_one_pdf(
            pdf_path,
            file_name=file_name,
            file_index=file_index,
            cfg=cfg,
            ocr_backend=ocr_backend,
            amount_column_keywords=amount_column_keywords,
            enable_llm_column_detection=enable_llm_column_detection,
            on_progress=file_progress,
        )
        file_summaries.append(file_summary)
        _progress(f"statement_file_{file_index}_done", base + span * 0.95)

    _progress("statement_aggregate", 0.99)
    report = _aggregate(file_summaries)
    _progress("done", 1.0)
    return report


def _process_one_pdf(
    pdf_path: str | Path,
    *,
    file_name: str,
    file_index: int,
    cfg: Settings,
    ocr_backend: str | None,
    amount_column_keywords: list[str] | None,
    enable_llm_column_detection: bool,
    on_progress: ProgressCb | None,
) -> StatementFileSummary:
    """处理单个 PDF:OCR + 表格抽取 + 求和。单文件异常 → error 字段,其他文件继续。"""
    try:
        reader = get_ocr_engine(ocr_backend)
        page_metas = get_page_metas(pdf_path, cfg.pdf_render_dpi)
        pages_blocks = reader.recognize(
            Path(pdf_path), page_metas, on_progress=on_progress
        )
        diagnostics = list(getattr(reader, "last_diagnostics", []))

        # 抽取所有结构化表格(block.label="table" 且 block.table is not None)
        tables_with_page: list[tuple[TableStructure, int]] = []
        for page_blocks in pages_blocks:
            for block in page_blocks:
                if _is_table_block(block):
                    tables_with_page.append((block.table, block.page_index))

        # 合并跨页续表
        merged_tables = merge_cross_page_tables(tables_with_page)

        # 预渲染各页 PNG(LLM 兜底列指认用;按需才渲染,避免无谓开销)
        page_pngs: dict[int, bytes] = {}
        if enable_llm_column_detection:
            try:
                png_list = render_pages(pdf_path, cfg.pdf_render_dpi)
                page_pngs = {i: png for i, png in enumerate(png_list)}
            except Exception as exc:  # noqa: BLE001
                logger.warning("render_pages failed for llm fallback: %s", exc)

        table_summaries: list[StatementTableSummary] = []
        recognition_needs_review = any(not d.reliable for d in diagnostics)

        for table_index, (table, page_index) in enumerate(merged_tables):
            summary = _summarize_one_table(
                table,
                file_index=file_index,
                file_name=file_name,
                table_index=table_index,
                page_index=page_index,
                user_keywords=amount_column_keywords,
                enable_llm_column_detection=enable_llm_column_detection,
                page_pngs=page_pngs,
            )
            table_summaries.append(summary)

        # 单 PDF 合计
        file_total = Decimal("0")
        for ts in table_summaries:
            for v in ts.column_sums.values():
                file_total += Decimal(str(v))

        return StatementFileSummary(
            file_index=file_index,
            file_name=file_name,
            total_amount=float(file_total),
            tables=table_summaries,
            recognition_status="needs_review" if recognition_needs_review else "reliable",
            recognition_diagnostics=diagnostics,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("statement file failed file=%s", file_name)
        return StatementFileSummary(
            file_index=file_index,
            file_name=file_name,
            total_amount=0.0,
            tables=[],
            recognition_status="needs_review",
            recognition_diagnostics=[],
            error=str(exc),
        )


def _summarize_one_table(
    table: TableStructure,
    *,
    file_index: int,
    file_name: str,
    table_index: int,
    page_index: int,
    user_keywords: list[str] | None,
    enable_llm_column_detection: bool,
    page_pngs: dict[int, bytes],
) -> StatementTableSummary:
    """对单张表做启发式列定位 → (失败时)LLM 兜底 → 代码求和。"""
    # 第 1 步:启发式
    summary = summarize_table(
        table,
        file_index=file_index,
        file_name=file_name,
        table_index=table_index,
        page_index=page_index,
        user_keywords=user_keywords,
    )

    # 启发式命中(有 column_sums 或 declared_totals)→ 直接用,不调 LLM
    if summary.column_source:
        return summary

    # 第 2 步:启发式失败,尝试 LLM 兜底
    if not enable_llm_column_detection:
        # 未启用兜底:重写 column_source 标记失败列(用全部 headers 标 none)
        summary.column_source = {
            h: "none" for h in table.headers if h and h.strip()
        }
        return summary

    if not table.headers:
        return summary

    png = page_pngs.get(page_index)
    if png is None:
        summary.column_source = {h: "none" for h in table.headers if h and h.strip()}
        return summary

    # 延迟导入(LLM 引擎实例化需要 api_base 配置)
    from .statement.llm_column_detect import llm_detect_amount_columns

    try:
        llm_result = llm_detect_amount_columns(png, list(table.headers))
    except Exception as exc:  # noqa: BLE001
        logger.warning("llm column detect error table=%s page=%s: %s", table_index, page_index, exc)
        llm_result = None

    if not llm_result:
        # LLM 也失败/无金额列 → 标 none
        summary.column_source = {h: "none" for h in table.headers if h and h.strip()}
        return summary

    # LLM 指认成功 → 用 LLM 给的列定位再代码求和
    summary = summarize_table(
        table,
        file_index=file_index,
        file_name=file_name,
        table_index=table_index,
        page_index=page_index,
        user_keywords=None,
        column_roles_override=dict(llm_result),
        column_source_override={idx: "llm" for idx in llm_result},
    )
    return summary


def _aggregate(file_summaries: list[StatementFileSummary]) -> StatementSummaryReport:
    """聚合多文件统计 → StatementSummaryReport。"""
    grand_total = Decimal("0")
    grand_by_column: dict[str, Decimal] = {}
    total_tables = 0
    total_items = 0
    column_detection_summary: dict[str, int] = {"heuristic": 0, "llm": 0, "none": 0}
    reasons: list[str] = []
    verdict = "clean"

    has_any_amount = False
    for fs in file_summaries:
        if fs.error:
            reasons.append(f"文件 {fs.file_name} 处理失败:{fs.error}")
            verdict = "needs_review"
            continue

        for ts in fs.tables:
            total_tables += 1
            total_items += len(ts.items)
            for col_name, value in ts.column_sums.items():
                grand_total += Decimal(str(value))
                grand_by_column[col_name] = grand_by_column.get(col_name, Decimal("0")) + Decimal(str(value))
                has_any_amount = True
            for src in ts.column_source.values():
                column_detection_summary[src] = column_detection_summary.get(src, 0) + 1

            # 声明核对:任一列不一致 → changed
            for col_name, match in ts.totals_match.items():
                if not match:
                    declared = ts.declared_totals.get(col_name, 0)
                    computed = ts.column_sums.get(col_name, 0)
                    reasons.append(
                        f"文件 {fs.file_name} 表 {ts.table_index} 列「{col_name}」"
                        f"声明合计 {declared} 与实算 {computed} 不一致"
                    )
                    verdict = "changed"

            # 列定位失败 → needs_review(优先级低于 changed)
            if any(src == "none" for src in ts.column_source.values()) and verdict != "changed":
                verdict = "needs_review"
                reasons.append(
                    f"文件 {fs.file_name} 表 {ts.table_index} 部分金额列定位失败,需人工复核"
                )

        if fs.recognition_status == "needs_review" and verdict == "clean":
            verdict = "needs_review"
            reasons.append(f"文件 {fs.file_name} OCR 质量需复核")

    # 完全无金额数据 → needs_review(可能是 OCR 没识别到表格)
    if not has_any_amount and verdict == "clean":
        verdict = "needs_review"
        reasons.append("未识别到任何金额数据,请确认 PDF 是否含表格或 OCR 是否成功")

    return StatementSummaryReport(
        files=file_summaries,
        grand_total=float(grand_total),
        grand_totals_by_column={k: float(v) for k, v in grand_by_column.items()},
        total_files=len(file_summaries),
        total_tables=total_tables,
        total_items=total_items,
        verdict=verdict,
        reasons=reasons,
        column_detection_summary=column_detection_summary,
    )


def _is_table_block(block: Block) -> bool:
    """判断 Block 是否为结构化表格(有 label=table 且携带 TableStructure)。"""
    return (
        getattr(block, "label", "") == "table"
        and getattr(block, "table", None) is not None
    )
