"""对帐单金额统计流水线集成测试。

用 mock OCR 引擎(让 recognize 返回构造好的 Block(label="table", table=TableStructure))
避开真实 LLM 调用,覆盖 pipeline 的所有分支。
"""
from unittest.mock import MagicMock

from document_comparison.models import (
    Block,
    PageRecognitionDiagnostic,
    TableStructure,
)
from document_comparison.statement_pipeline import run_statement_pipeline


def _make_table_block(headers, rows, page_index=0):
    """构造一个 label=table 的 Block。"""
    return Block(
        block_id=f"p{page_index}-t0",
        page_index=page_index,
        label="table",
        table=TableStructure(headers=headers, rows=rows),
    )


def _make_mock_reader(tables_per_page):
    """构造 mock OCR 引擎。

    tables_per_page: list[list[Block]] — 每页的 Block 列表。
    返回的 mock 有 last_diagnostics 属性(空列表)。
    """
    reader = MagicMock()
    reader.recognize.return_value = tables_per_page
    reader.last_diagnostics = []
    return reader


def _patch_get_ocr_engine(monkeypatch, reader):
    """让 get_ocr_engine 返回我们的 mock reader。"""
    import document_comparison.statement_pipeline as sp
    monkeypatch.setattr(sp, "get_ocr_engine", lambda backend=None: reader)


def _patch_page_metas(monkeypatch, num_pages):
    """让 get_page_metas 返回占位元数据(数量匹配)。"""
    from document_comparison.models import PageMeta
    metas = [PageMeta(page_index=i, width_px=595, height_px=842) for i in range(num_pages)]
    import document_comparison.statement_pipeline as sp
    monkeypatch.setattr(sp, "get_page_metas", lambda path, dpi=200: metas)


def _disable_render(monkeypatch):
    """禁用 render_pages(避免真实 PDF 渲染;LLM 兜底测试单独覆盖)。"""
    import document_comparison.statement_pipeline as sp
    monkeypatch.setattr(sp, "render_pages", lambda path, dpi=200: [])


def _make_real_pdf(tmp_path, name="x.pdf", num_pages=1):
    """构造一个最小真实 PDF(供 render_pages / count_pages 之类调用)。
    pipeline 测试主要 mock OCR,但若需要真实 PDF 文件路径时使用。"""
    import io
    try:
        import pymupdf as fitz
    except ImportError:
        import fitz  # type: ignore
    buf = io.BytesIO()
    pdf = fitz.open()
    for _ in range(num_pages):
        pdf.new_page(width=595, height=842)
    pdf.save(buf)
    pdf.close()
    buf.seek(0)
    path = tmp_path / name
    path.write_bytes(buf.read())
    return path


# —— 单 PDF 单表 ——

def test_single_pdf_single_table_heuristic(monkeypatch, tmp_path):
    """启发式列定位命中 → 不调 LLM,代码求和。"""
    pdf_path = _make_real_pdf(tmp_path, "a.pdf", num_pages=1)
    block = _make_table_block(
        headers=["项目", "金额"],
        rows=[["A", "30000元"], ["B", "70000元"]],
        page_index=0,
    )
    reader = _make_mock_reader([[block]])
    _patch_get_ocr_engine(monkeypatch, reader)
    _patch_page_metas(monkeypatch, 1)
    _disable_render(monkeypatch)

    report = run_statement_pipeline([pdf_path], ["a.pdf"])
    assert report.total_files == 1
    assert report.total_tables == 1
    assert report.grand_total == 100000.0
    assert report.files[0].total_amount == 100000.0
    assert report.files[0].error is None
    assert report.column_detection_summary.get("heuristic") == 1
    assert report.column_detection_summary.get("llm", 0) == 0


def test_heuristic_failure_no_llm_flag_marks_needs_review(monkeypatch, tmp_path):
    """启发式失败 + 关闭 LLM 兜底 → column_source="none",verdict=needs_review。"""
    pdf_path = _make_real_pdf(tmp_path, "a.pdf", num_pages=1)
    block = _make_table_block(
        headers=["日期", "项目", "备注"],  # 无金额列关键词
        rows=[["2024", "A", "100元"]],
        page_index=0,
    )
    reader = _make_mock_reader([[block]])
    _patch_get_ocr_engine(monkeypatch, reader)
    _patch_page_metas(monkeypatch, 1)
    _disable_render(monkeypatch)

    report = run_statement_pipeline(
        [pdf_path], ["a.pdf"],
        enable_llm_column_detection=False,
    )
    assert report.verdict == "needs_review"
    assert report.column_detection_summary.get("none", 0) >= 1
    assert report.grand_total == 0.0


def test_cross_page_tables_merged(monkeypatch, tmp_path):
    """两页相同 headers 的表 → 合一张统计。"""
    pdf_path = _make_real_pdf(tmp_path, "a.pdf", num_pages=2)
    t1 = _make_table_block(
        headers=["项目", "金额"],
        rows=[["A", "30000元"]],
        page_index=0,
    )
    t2 = _make_table_block(
        headers=["项目", "金额"],
        rows=[["B", "70000元"]],
        page_index=1,
    )
    reader = _make_mock_reader([[t1], [t2]])
    _patch_get_ocr_engine(monkeypatch, reader)
    _patch_page_metas(monkeypatch, 2)
    _disable_render(monkeypatch)

    report = run_statement_pipeline([pdf_path], ["a.pdf"])
    assert report.total_tables == 1  # 合并后 1 张逻辑表
    assert report.grand_total == 100000.0


def test_multiple_tables_same_page(monkeypatch, tmp_path):
    """单页两张表(不同 headers)各自统计,不被合并。"""
    pdf_path = _make_real_pdf(tmp_path, "a.pdf", num_pages=1)
    t1 = _make_table_block(
        headers=["项目", "金额"],
        rows=[["A", "100元"]],
        page_index=0,
    )
    # 第二张表用不同 headers,避免被跨页续表逻辑误合并
    t2 = _make_table_block(
        headers=["日期", "金额"],
        rows=[["2024-01", "200元"]],
        page_index=0,
    )
    reader = _make_mock_reader([[t1, t2]])
    _patch_get_ocr_engine(monkeypatch, reader)
    _patch_page_metas(monkeypatch, 1)
    _disable_render(monkeypatch)

    report = run_statement_pipeline([pdf_path], ["a.pdf"])
    assert report.total_tables == 2
    assert report.grand_total == 300.0


def test_declared_total_match(monkeypatch, tmp_path):
    """声明合计一致 → verdict=clean(数据存在 + 匹配)。"""
    pdf_path = _make_real_pdf(tmp_path, "a.pdf", num_pages=1)
    block = _make_table_block(
        headers=["项目", "金额"],
        rows=[["A", "30000元"], ["B", "70000元"], ["合计", "100000元"]],
        page_index=0,
    )
    reader = _make_mock_reader([[block]])
    _patch_get_ocr_engine(monkeypatch, reader)
    _patch_page_metas(monkeypatch, 1)
    _disable_render(monkeypatch)

    report = run_statement_pipeline([pdf_path], ["a.pdf"])
    assert report.verdict == "clean"
    assert report.grand_total == 100000.0


def test_declared_total_mismatch_marks_changed(monkeypatch, tmp_path):
    """声明合计不一致 → verdict=changed。"""
    pdf_path = _make_real_pdf(tmp_path, "a.pdf", num_pages=1)
    block = _make_table_block(
        headers=["项目", "金额"],
        rows=[["A", "30000元"], ["B", "70000元"], ["合计", "99999元"]],
        page_index=0,
    )
    reader = _make_mock_reader([[block]])
    _patch_get_ocr_engine(monkeypatch, reader)
    _patch_page_metas(monkeypatch, 1)
    _disable_render(monkeypatch)

    report = run_statement_pipeline([pdf_path], ["a.pdf"])
    assert report.verdict == "changed"
    assert any("不一致" in r for r in report.reasons)


# —— 多文件聚合 ——

def test_multi_files_aggregation(monkeypatch, tmp_path):
    """2 个 PDF → grand_total = file1.total + file2.total。"""
    pdf1 = _make_real_pdf(tmp_path, "a.pdf", num_pages=1)
    pdf2 = _make_real_pdf(tmp_path, "b.pdf", num_pages=1)
    block1 = _make_table_block(
        headers=["项目", "金额"],
        rows=[["A", "30000元"]],
        page_index=0,
    )
    block2 = _make_table_block(
        headers=["项目", "金额"],
        rows=[["B", "70000元"]],
        page_index=0,
    )
    # get_ocr_engine 每次 PDF 调用一次,返回不同 reader
    reader1 = _make_mock_reader([[block1]])
    reader2 = _make_mock_reader([[block2]])
    readers = iter([reader1, reader2])
    import document_comparison.statement_pipeline as sp
    monkeypatch.setattr(sp, "get_ocr_engine", lambda backend=None: next(readers))
    _patch_page_metas(monkeypatch, 1)
    _disable_render(monkeypatch)

    report = run_statement_pipeline([pdf1, pdf2], ["a.pdf", "b.pdf"])
    assert report.total_files == 2
    assert report.grand_total == 100000.0
    assert report.files[0].total_amount == 30000.0
    assert report.files[1].total_amount == 70000.0


def test_single_file_failure_others_continue(monkeypatch, tmp_path):
    """单文件失败(模拟 OCR 异常)→ 其他文件继续,verdict=needs_review,error 有值。"""
    pdf1 = _make_real_pdf(tmp_path, "a.pdf", num_pages=1)
    pdf2 = _make_real_pdf(tmp_path, "b.pdf", num_pages=1)
    block2 = _make_table_block(
        headers=["项目", "金额"],
        rows=[["B", "70000元"]],
        page_index=0,
    )

    # reader1.recognize 抛异常;reader2 正常
    reader1 = MagicMock()
    reader1.recognize.side_effect = RuntimeError("OCR 服务不可用")
    reader2 = _make_mock_reader([[block2]])
    readers = iter([reader1, reader2])
    import document_comparison.statement_pipeline as sp
    monkeypatch.setattr(sp, "get_ocr_engine", lambda backend=None: next(readers))
    _patch_page_metas(monkeypatch, 1)
    _disable_render(monkeypatch)

    report = run_statement_pipeline([pdf1, pdf2], ["a.pdf", "b.pdf"])
    assert report.total_files == 2
    assert report.files[0].error is not None
    assert "OCR" in report.files[0].error or "不可用" in report.files[0].error
    assert report.files[1].error is None
    assert report.files[1].total_amount == 70000.0
    assert report.grand_total == 70000.0  # 只算成功的
    assert report.verdict == "needs_review"
    assert any("失败" in r for r in report.reasons)


def test_no_amount_data_marks_needs_review(monkeypatch, tmp_path):
    """完全无金额数据 → needs_review。"""
    pdf_path = _make_real_pdf(tmp_path, "a.pdf", num_pages=1)
    block = _make_table_block(
        headers=["日期", "项目", "备注"],  # 无金额列,且 LLM 兜底关闭
        rows=[["2024", "A", "x"]],
        page_index=0,
    )
    reader = _make_mock_reader([[block]])
    _patch_get_ocr_engine(monkeypatch, reader)
    _patch_page_metas(monkeypatch, 1)
    _disable_render(monkeypatch)

    report = run_statement_pipeline(
        [pdf_path], ["a.pdf"],
        enable_llm_column_detection=False,
    )
    assert report.verdict == "needs_review"
    assert report.grand_total == 0.0


def test_recognition_unreliable_marks_needs_review(monkeypatch, tmp_path):
    """OCR 质量诊断 unreliable → verdict=needs_review。"""
    pdf_path = _make_real_pdf(tmp_path, "a.pdf", num_pages=1)
    block = _make_table_block(
        headers=["项目", "金额"],
        rows=[["A", "100元"]],
        page_index=0,
    )
    reader = MagicMock()
    reader.recognize.return_value = [[block]]
    reader.last_diagnostics = [
        PageRecognitionDiagnostic(
            page_index=0, source="fallback", reliable=False,
            reasons=["OCR 返回内容为空或字符过少"],
        )
    ]
    _patch_get_ocr_engine(monkeypatch, reader)
    _patch_page_metas(monkeypatch, 1)
    _disable_render(monkeypatch)

    report = run_statement_pipeline([pdf_path], ["a.pdf"])
    assert report.files[0].recognition_status == "needs_review"
    assert report.verdict == "needs_review"


# —— LLM 兜底 ——

def test_llm_fallback_picks_up_columns(monkeypatch, tmp_path):
    """启发式失败 + LLM 兜底成功 → column_source 标 "llm",代码求和。"""
    pdf_path = _make_real_pdf(tmp_path, "a.pdf", num_pages=1)
    block = _make_table_block(
        headers=["日期", "Money"],  # 启发式不命中 Money
        rows=[["2024", "30000元"]],
        page_index=0,
    )
    reader = _make_mock_reader([[block]])
    _patch_get_ocr_engine(monkeypatch, reader)
    _patch_page_metas(monkeypatch, 1)

    # render_pages 返回一个占位 PNG(让 pipeline 进入 LLM 路径)
    import document_comparison.statement_pipeline as sp
    monkeypatch.setattr(sp, "render_pages", lambda path, dpi=200: [b"fake-png"])

    # mock llm_detect_amount_columns 指认 index=1 为 amount 列
    import document_comparison.statement.llm_column_detect as lcd
    monkeypatch.setattr(lcd, "llm_detect_amount_columns", lambda png, headers: {1: "amount"})

    report = run_statement_pipeline(
        [pdf_path], ["a.pdf"],
        enable_llm_column_detection=True,
    )
    assert report.grand_total == 30000.0
    assert report.column_detection_summary.get("llm") == 1
    assert report.column_detection_summary.get("heuristic", 0) == 0


def test_llm_fallback_failure_marks_needs_review(monkeypatch, tmp_path):
    """启发式失败 + LLM 也失败 → column_source="none",verdict=needs_review。"""
    pdf_path = _make_real_pdf(tmp_path, "a.pdf", num_pages=1)
    block = _make_table_block(
        headers=["日期", "Money"],
        rows=[["2024", "30000元"]],
        page_index=0,
    )
    reader = _make_mock_reader([[block]])
    _patch_get_ocr_engine(monkeypatch, reader)
    _patch_page_metas(monkeypatch, 1)

    import document_comparison.statement_pipeline as sp
    monkeypatch.setattr(sp, "render_pages", lambda path, dpi=200: [b"fake-png"])

    # mock LLM 返回 None(失败)
    import document_comparison.statement.llm_column_detect as lcd
    monkeypatch.setattr(lcd, "llm_detect_amount_columns", lambda png, headers: None)

    report = run_statement_pipeline([pdf_path], ["a.pdf"])
    assert report.verdict == "needs_review"
    assert report.column_detection_summary.get("none", 0) >= 1


# —— 进度回调 ——

def test_progress_callback_invoked(monkeypatch, tmp_path):
    """on_progress 被调用;起始与终态正确。
    注:mock OCR 不回调 on_progress,所以中间值不严格单调;这里只校验关键里程碑。"""
    pdf_path = _make_real_pdf(tmp_path, "a.pdf", num_pages=1)
    block = _make_table_block(
        headers=["项目", "金额"],
        rows=[["A", "100元"]],
        page_index=0,
    )
    reader = _make_mock_reader([[block]])
    _patch_get_ocr_engine(monkeypatch, reader)
    _patch_page_metas(monkeypatch, 1)
    _disable_render(monkeypatch)

    progress_values: list[float] = []
    stages: list[str] = []
    def on_progress(stage, frac):
        stages.append(stage)
        progress_values.append(frac)

    run_statement_pipeline([pdf_path], ["a.pdf"], on_progress=on_progress)
    assert stages[0] == "statement_start"
    assert stages[-1] == "done"
    assert progress_values[0] == 0.02   # statement_start
    assert progress_values[-1] == 1.0   # done


# —— 空输入 ——

def test_empty_pdf_list_returns_empty_report():
    report = run_statement_pipeline([], [])
    assert report.total_files == 0
    assert report.grand_total == 0.0
    assert report.files == []
