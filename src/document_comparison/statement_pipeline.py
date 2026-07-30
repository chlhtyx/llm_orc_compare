"""金额统计流水线(独立逻辑,与 TamperReport / TextDiffReport 体系完全隔离)。

流程(多文件串行):
  对每个 PDF:
    ① get_ocr_engine → TrustedPDFReader(原生优先 + 视觉 OCR 逐页降级)
    ② 抽取所有 label="table" 且 table is not None 的 Block
    ③ merge_cross_page_tables 合并跨页续表
    ④ 对每张表三级级联抽取金额:
       a. 启发式 detect_amount_columns 命中 → 代码求和(对帐单场景,免费确定)
       b. 启发式失败 + enable_llm_column_detection → llm_detect_amount_columns
          兜底指认列,再代码求和
       c. 列指认仍抽空(典型:发票纯数字无单位 + 表头乱码)→ llm_extract_amounts
          直接抽取数据行金额,逐值 grounding 校验(OCR 文本逐字溯源)通过后由
          代码累加;失败/无金额 → column_source="none",标 needs_review
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
    StatementAmountItem,
    StatementFileSummary,
    StatementSummaryReport,
    StatementTableSummary,
    TableStructure,
)
from .ocr import get_ocr_engine
from .parsing.pdf import get_page_metas, render_pages
from .statement.amount_column import (
    merge_cross_page_tables,
    summarize_table,
)
from .statement.invoice_layout import looks_like_invoice, parse_invoice_page

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

        # 全电发票专用解析:pymupdf/OCR 会把发票明细表压扁成纯文本单元格,
        # 用确定性坐标解析器重建明细表(仅对发票页面生效,对帐单等回退原表)。
        page_texts = {
            i: "\n".join(b.content for b in blocks if b.content)
            for i, blocks in enumerate(pages_blocks)
        }
        invoice_pages = [i for i, t in page_texts.items() if looks_like_invoice(t)]
        if invoice_pages:
            tables_with_page = _apply_invoice_layout(
                pdf_path, tables_with_page, invoice_pages
            )

        # 顶层诊断:OCR 产出了什么、有没有 table block。定位"识别不到金额"时,这是
        # 判断卡在哪一层(OCR 没出表 / 出了表但抽空 / LLM 兜底失败)的第一手信息。
        page_block_summary = {
            i: [(b.label, bool(b.table), len(b.content)) for b in blocks]
            for i, blocks in enumerate(pages_blocks)
        }
        logger.info(
            "statement ocr output file=%s pages=%s table_blocks=%s invoice_pages=%s blocks_per_page=%s",
            file_name, len(pages_blocks), len(tables_with_page), invoice_pages, page_block_summary,
        )

        # 合并跨页续表
        merged_tables = merge_cross_page_tables(tables_with_page)

        # 预渲染各页 PNG(LLM 兜底列指认/金额抽取用;按需才渲染,避免无谓开销)
        page_pngs: dict[int, bytes] = {}
        if enable_llm_column_detection:
            try:
                png_list = render_pages(pdf_path, cfg.pdf_render_dpi)
                page_pngs = {i: png for i, png in enumerate(png_list)}
            except Exception as exc:  # noqa: BLE001
                logger.warning("render_pages failed for llm fallback: %s", exc)

        # 各页 OCR 纯文本(所有 block.content 拼接),作为 LLM 金额抽取的 grounding
        # 逐字溯源依据:LLM 返回的每个金额必须能在其中找到,否则丢弃。
        # (page_texts 已在上方发票识别时构建,这里复用)
        page_groundings = page_texts

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
                page_groundings=page_groundings,
            )
            table_summaries.append(summary)

        # 整页文本兜底:OCR 未把内容解析成结构化表格(常见于图片型发票/收据),但 OCR
        # 文本里可能含金额。此时把每页 OCR 文本作为一张"隐式表"喂给 LLM 金额抽取,
        # 避免因"没出 table block"直接判 needs_review 而漏掉所有金额。
        if (
            enable_llm_column_detection
            and not merged_tables
            and any(page_groundings.values())
        ):
            logger.info(
                "statement whole-page fallback file=%s (no table block, try LLM on page text)",
                file_name,
            )
            for page_index, page_text in page_groundings.items():
                if not page_text or not page_text.strip():
                    continue
                whole_summary = _extract_whole_page_amounts(
                    page_text,
                    png=page_pngs.get(page_index, b""),
                    file_index=file_index,
                    file_name=file_name,
                    page_index=page_index,
                    table_index=len(table_summaries),
                )
                if whole_summary is not None:
                    table_summaries.append(whole_summary)

        # 单 PDF 合计(含税口径):用各表 tax_inclusive_total 而非所有 column_sums,
        # 避免把 amount+tax 重复计入(有含税列时也已排除不含税/税额)。
        file_total = Decimal("0")
        for ts in table_summaries:
            file_total += Decimal(str(ts.tax_inclusive_total))

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


def _has_amounts(summary: StatementTableSummary) -> bool:
    """判断 summary 是否真正抽到了金额(而非仅完成列定位)。

    发票场景的关键:表头"金额"列能被启发式定位(column_source 非空),但单元格是
    纯数字无单位,正则抽不出 → column_sums/declared_totals 都空。此时不能认为
    "已处理完成",必须继续走 LLM 兜底。
    """
    return bool(summary.column_sums or summary.declared_totals)


def _log_summary_stage(stage: str, summary: StatementTableSummary) -> None:
    """记录各级抽取的诊断信息,便于分析"识别不到金额"的根因。

    日志含:列定位方式(column_source)、实抽金额(column_sums)、声明合计(declared_totals)、
    明细数。不记录单元格原文(避免敏感金额明细入日志)。
    """
    logger.info(
        "statement extract stage=%s file=%s table=%s page=%s "
        "column_source=%s column_sums=%s declared_totals=%s items=%s headers=%s",
        stage,
        summary.file_name,
        summary.table_index,
        summary.page_index,
        summary.column_source,
        summary.column_sums,
        summary.declared_totals,
        len(summary.items),
        summary.headers,
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
    page_groundings: dict[int, str],
) -> StatementTableSummary:
    """对单张表做三级级联金额抽取,代码求和。

    1. 启发式 detect_amount_columns(对帐单:带元/万元/¥/中文大写)
    2. LLM 列指认 → 代码按列求和(表头非标但单元格金额仍带单位)
    3. LLM 金额抽取 + grounding 校验 → 代码累加(发票:纯数字无单位 + 表头乱码)
    三级全部抽空 → column_source="none",标 needs_review。
    """
    # 第 1 步:启发式
    summary = summarize_table(
        table,
        file_index=file_index,
        file_name=file_name,
        table_index=table_index,
        page_index=page_index,
        user_keywords=user_keywords,
    )
    # 启发式判定:看是否真正抽到了金额(而非仅列定位成功)。发票场景表头"金额"列
    # 能定位,但单元格是纯数字无单位,正则抽不出 → column_sums 空,必须继续兜底。
    _log_summary_stage("heuristic", summary)

    if _has_amounts(summary):
        return summary

    # 第 2 步:启发式未抽到金额,尝试 LLM 列指认
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
        logger.info(
            "statement column fallback file_index=%s table_index=%s page_index=%s header_count=%s",
            file_index, table_index, page_index, len(table.headers),
        )
        llm_result = llm_detect_amount_columns(
            png,
            list(table.headers),
            trace_context={
                "file_index": file_index,
                "table_index": table_index,
                "page_index": page_index,
            },
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("llm column detect error table=%s page=%s: %s", table_index, page_index, exc)
        llm_result = None

    if llm_result:
        # 列指认成功 → 用 LLM 给的列定位再代码求和
        col_summary = summarize_table(
            table,
            file_index=file_index,
            file_name=file_name,
            table_index=table_index,
            page_index=page_index,
            user_keywords=None,
            column_roles_override=dict(llm_result),
            column_source_override={idx: "llm" for idx in llm_result},
        )
        _log_summary_stage("llm-column", col_summary)
        # 列指认定位到了列,但单元格仍是纯数字无单位时仍抽不到钱 → 继续走第 3 步
        if _has_amounts(col_summary):
            return col_summary
        summary = col_summary

    # 第 3 步:列指认仍未抽到金额(典型:发票纯数字无单位 + 表头乱码)→ LLM 直接抽取
    # 数据行金额,逐值 grounding 校验通过后由代码累加。
    amount_items = _llm_extract_amount_items(
        table,
        png=png,
        page_groundings=page_groundings.get(page_index, ""),
        file_index=file_index,
        file_name=file_name,
        table_index=table_index,
        page_index=page_index,
    )
    if amount_items:
        # 把通过 grounding 校验的金额写入 column_sums(代码累加,不破坏聚合路径);
        # column_source 标 "llm" 表明本表金额由 LLM 兜底抽取得到。
        col_name = amount_items[0].column or "金额(llm)"
        total = Decimal("0")
        for it in amount_items:
            total += Decimal(str(it.value))
        summary.column_sums = {col_name: float(total)}
        summary.items = amount_items
        summary.column_source = {col_name: "llm"}
        # LLM 抽取路径:含税口径统一标「LLM 抽取」(列角色体系不适用,已是数据行金额)
        summary.tax_inclusive_total = float(total)
        summary.tax_inclusive_method = "LLM 抽取"
        _log_summary_stage("llm-amount", summary)
        return summary

    # 三级全部抽空 → 标 none(needs_review)
    summary.column_source = {h: "none" for h in table.headers if h and h.strip()}
    _log_summary_stage("exhausted", summary)
    return summary


def _llm_extract_amount_items(
    table: TableStructure,
    *,
    png: bytes,
    page_groundings: str,
    file_index: int,
    file_name: str,
    table_index: int,
    page_index: int,
) -> list[StatementAmountItem]:
    """第 3 级:LLM 直接抽取数据行金额(仅返回通过 grounding 校验的项)。

    失败/异常返回空 list(由调用方标 needs_review)。求和仍由调用方用 Decimal 完成。
    """
    # 延迟导入(LLM 引擎实例化需要 api_base 配置)
    from .statement.llm_amount_extract import llm_extract_amounts

    try:
        logger.info(
            "statement amount fallback file_index=%s table_index=%s page_index=%s header_count=%s row_count=%s",
            file_index, table_index, page_index, len(table.headers), len(table.rows),
        )
        return llm_extract_amounts(
            png,
            list(table.headers),
            list(table.rows),
            page_groundings,
            file_index=file_index,
            file_name=file_name,
            table_index=table_index,
            page_index=page_index,
        ) or []
    except Exception as exc:  # noqa: BLE001
        logger.warning("llm amount extract error table=%s page=%s: %s", table_index, page_index, exc)
        return []


def _extract_whole_page_amounts(
    page_text: str,
    *,
    png: bytes,
    file_index: int,
    file_name: str,
    page_index: int,
    table_index: int,
) -> StatementTableSummary | None:
    """整页文本兜底:OCR 未产出结构化表格时,把整页 OCR 文本作为"隐式表"喂给
    LLM 金额抽取(图片型发票/收据的典型场景)。

    grounding 即 page_text 本身,LLM 返回的每个金额必须能在其中逐字溯源。
    成功返回带 column_sums 的 summary;无金额/失败返回 None。
    """
    from .statement.llm_amount_extract import llm_extract_amounts

    # 把整页文本按行拆成"隐式表":headers 占位,rows 每行一段文本,
    # 让 LLM 在全文范围内抽金额(而非限定在某张结构化表里)。
    rows = [[line] for line in page_text.splitlines() if line.strip()]
    if not rows:
        return None
    try:
        items = llm_extract_amounts(
            png,
            ["全文"],
            rows,
            page_text,
            file_index=file_index,
            file_name=file_name,
            table_index=table_index,
            page_index=page_index,
        ) or []
    except Exception as exc:  # noqa: BLE001
        logger.warning("whole-page amount extract error file=%s page=%s: %s", file_name, page_index, exc)
        return None

    if not items:
        logger.info(
            "whole-page amount extract no amounts file=%s page=%s",
            file_name, page_index,
        )
        return None

    col_name = items[0].column or "金额(llm)"
    total = Decimal("0")
    for it in items:
        total += Decimal(str(it.value))
    summary = StatementTableSummary(
        file_index=file_index,
        file_name=file_name,
        table_index=table_index,
        page_index=page_index,
        headers=["全文"],
        column_sums={col_name: float(total)},
        items=items,
        column_source={col_name: "llm"},
        tax_inclusive_total=float(total),
        tax_inclusive_method="LLM 抽取",
    )
    _log_summary_stage("whole-page", summary)
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
            # grand_total 用含税口径(各表 tax_inclusive_total),避免重复计入
            grand_total += Decimal(str(ts.tax_inclusive_total))
            if ts.tax_inclusive_total > 0 or ts.column_sums:
                has_any_amount = True
            # grand_totals_by_column 仍按列名分桶展示构成(价税合计/金额/税额各行)
            for col_name, value in ts.column_sums.items():
                grand_by_column[col_name] = grand_by_column.get(col_name, Decimal("0")) + Decimal(str(value))
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


def _apply_invoice_layout(
    pdf_path: str | Path,
    tables_with_page: list[tuple[TableStructure, int]],
    invoice_pages: list[int],
) -> list[tuple[TableStructure, int]]:
    """对发票页面用坐标解析器重建明细表,替换 OCR/pymupdf 压扁的表。

    全电发票明细表会被 pymupdf ``find_tables`` 压扁成纯文本单元格,无法用通用列定位。
    本函数重开 PDF 取原生文本层 span 坐标,用 ``invoice_layout.parse_invoice_page``
    确定性重建明细 TableStructure。对帐单等非发票页面保持原表不动。

    策略:发票页面的原表全部丢弃(发票的 pymupdf 表是错乱的),换成解析器产出;
    非发票页面的表原样保留。
    """
    try:
        import pymupdf as fitz  # 延迟导入,避免无 PDF 依赖时 import 失败
    except ImportError:  # pragma: no cover
        try:
            import fitz  # type: ignore
        except ImportError:
            logger.warning("invoice_layout: pymupdf 不可用,回退原表")
            return tables_with_page

    invoice_page_set = set(invoice_pages)
    # 保留非发票页面的原表
    result: list[tuple[TableStructure, int]] = [
        (t, p) for t, p in tables_with_page if p not in invoice_page_set
    ]

    try:
        doc = fitz.open(str(pdf_path))
    except Exception as exc:  # noqa: BLE001
        logger.warning("invoice_layout: 打开 PDF 失败 %s,回退原表: %s", pdf_path, exc)
        return tables_with_page

    try:
        for page_index in invoice_pages:
            if page_index >= doc.page_count:
                continue
            table, grand_total = parse_invoice_page(doc[page_index])
            if table is not None:
                result.append((table, page_index))
                logger.info(
                    "invoice_layout 应用了发票坐标解析 page=%s 价税合计=%s",
                    page_index, grand_total,
                )
            else:
                logger.info("invoice_layout 未识别出明细表 page=%s,保留原表", page_index)
                # 回退:该发票页保留原 OCR 表(若有)
                for t, p in tables_with_page:
                    if p == page_index:
                        result.append((t, p))
    finally:
        doc.close()

    return result
