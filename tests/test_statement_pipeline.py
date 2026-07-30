"""金额统计流水线集成测试。

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
    """禁用 render_page(避免真实 PDF 渲染;LLM 兜底测试单独覆盖)。"""
    import document_comparison.statement_pipeline as sp
    monkeypatch.setattr(sp, "render_page", lambda path, page_index, dpi=200: b"")


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


def test_tax_inclusive_column_no_double_count(monkeypatch, tmp_path):
    """含税金额统计:有价税合计列时 grand_total 用含税合计,不重复计入金额+税额。"""
    pdf_path = _make_real_pdf(tmp_path, "a.pdf", num_pages=1)
    block = _make_table_block(
        headers=["项目", "金额", "税额", "价税合计"],
        rows=[
            ["A", "100元", "13元", "113元"],
            ["B", "200元", "26元", "226元"],
        ],
        page_index=0,
    )
    reader = _make_mock_reader([[block]])
    _patch_get_ocr_engine(monkeypatch, reader)
    _patch_page_metas(monkeypatch, 1)
    _disable_render(monkeypatch)

    report = run_statement_pipeline([pdf_path], ["a.pdf"])
    # 含税合计 = 价税合计列 = 113 + 226 = 339(不是 100+200+13+26+113+226)
    assert report.grand_total == 339.0
    assert report.files[0].total_amount == 339.0
    # 按列分桶仍展示构成(用于「按金额列汇总」)
    assert report.grand_totals_by_column == {"金额": 300.0, "税额": 39.0, "价税合计": 339.0}
    # 单表含税口径
    assert report.files[0].tables[0].tax_inclusive_total == 339.0
    assert report.files[0].tables[0].tax_inclusive_method == "含税/价税合计列"


def test_tax_inclusive_amount_plus_tax_no_inclusive_column(monkeypatch, tmp_path):
    """含税金额统计:无含税列时 含税=金额(不含税)+税额。"""
    pdf_path = _make_real_pdf(tmp_path, "a.pdf", num_pages=1)
    block = _make_table_block(
        headers=["项目", "金额", "税额"],
        rows=[["A", "100元", "13元"], ["B", "200元", "26元"]],
        page_index=0,
    )
    reader = _make_mock_reader([[block]])
    _patch_get_ocr_engine(monkeypatch, reader)
    _patch_page_metas(monkeypatch, 1)
    _disable_render(monkeypatch)

    report = run_statement_pipeline([pdf_path], ["a.pdf"])
    # 含税 = 300 + 39 = 339
    assert report.grand_total == 339.0
    assert report.files[0].tables[0].tax_inclusive_total == 339.0
    assert report.files[0].tables[0].tax_inclusive_method == "金额(不含税)列 + 税额列"


# —— 全电发票坐标解析器集成 ——
# parse_invoice_page 的坐标解析逻辑已在 test_invoice_layout.py 用 _FakePage 充分测试;
# 此处只验证 pipeline 的"发票分发"接线:OCR 文本含发票特征时,丢弃 OCR 压扁的乱表,
# 改用 invoice_layout 产出。通过 mock parse_invoice_page 返回已知表来隔离 PDF 渲染
# (测试环境 pymupdf 无中文字体,无法渲染可读中文)。

def test_invoice_dispatch_replaces_ocr_table(monkeypatch, tmp_path):
    """OCR 文本像发票 → 丢弃 OCR 乱表,改用 invoice_layout 产出的规整表。"""
    import document_comparison.statement_pipeline as sp
    from document_comparison.models import TableStructure

    pdf_path = _make_real_pdf(tmp_path, "inv.pdf", num_pages=1)
    # OCR 返回"压扁"的乱表(模拟 pymupdf find_tables 对发票的失效结果)
    bad_block = _make_table_block(
        headers=["购买方信息"],
        rows=[["商品A 100.00 13.00 商品B 200.00 26.00"]],
        page_index=0,
    )
    reader = _make_mock_reader([[bad_block]])
    _patch_get_ocr_engine(monkeypatch, reader)
    _patch_page_metas(monkeypatch, 1)
    _disable_render(monkeypatch)

    # 让 OCR 文本含发票特征(触发 looks_like_invoice)
    bad_block.content = "电⼦发票（普通发票）\n价税合计（大写）"

    # mock parse_invoice_page 返回规整发票表(金额+税额两列),隔离坐标解析细节
    good_table = TableStructure(
        headers=["项目名称", "金额", "税额"],
        rows=[["商品A", "100.00元", "13.00元"], ["商品B", "200.00元", "26.00元"]],
    )
    monkeypatch.setattr(
        sp, "parse_invoice_page", lambda page: (good_table, 339.00)
    )

    report = run_statement_pipeline([pdf_path], ["inv.pdf"])
    # 发票价税合计 = 金额(300) + 税额(39) = 339
    assert report.grand_total == 339.0
    assert report.files[0].total_amount == 339.0
    # 用的是发票表(3 列),不是 OCR 的 1 列乱表
    assert report.files[0].tables[0].headers == ["项目名称", "金额", "税额"]
    assert report.files[0].tables[0].tax_inclusive_method == "金额(不含税)列 + 税额列"


def test_non_invoice_not_dispatched_to_layout(monkeypatch, tmp_path):
    """对帐单(非发票)不走坐标解析器,用 OCR 产出的原表。"""
    pdf_path = _make_real_pdf(tmp_path, "stmt.pdf", num_pages=1)
    block = _make_table_block(
        headers=["项目", "金额"],
        rows=[["A", "30000元"]],
        page_index=0,
    )
    reader = _make_mock_reader([[block]])
    _patch_get_ocr_engine(monkeypatch, reader)
    _patch_page_metas(monkeypatch, 1)
    _disable_render(monkeypatch)

    report = run_statement_pipeline([pdf_path], ["stmt.pdf"])
    # 对帐单走原表,金额 30000
    assert report.grand_total == 30000.0
    assert report.files[0].tables[0].tax_inclusive_method == "金额列"


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

    # render_page 返回一个占位 PNG(让 pipeline 进入 LLM 路径)
    import document_comparison.statement_pipeline as sp
    monkeypatch.setattr(sp, "render_page", lambda path, page_index, dpi=200: b"fake-png")

    # mock llm_detect_amount_columns 指认 index=1 为 amount 列
    import document_comparison.statement.llm_column_detect as lcd
    monkeypatch.setattr(
        lcd, "llm_detect_amount_columns", lambda png, headers, **kwargs: {1: "amount"}
    )

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
    monkeypatch.setattr(sp, "render_page", lambda path, page_index, dpi=200: b"fake-png")

    # mock 列指认返回 None(失败)
    import document_comparison.statement.llm_column_detect as lcd
    monkeypatch.setattr(
        lcd, "llm_detect_amount_columns", lambda png, headers, **kwargs: None
    )
    # mock 第三级 LLM 金额抽取也失败(空 list)→ 三级全空
    import document_comparison.statement.llm_amount_extract as lae
    monkeypatch.setattr(
        lae, "llm_extract_amounts", lambda *a, **k: None,
    )

    report = run_statement_pipeline([pdf_path], ["a.pdf"])
    assert report.verdict == "needs_review"
    assert report.column_detection_summary.get("none", 0) >= 1


# —— 第三级:LLM 金额抽取(发票纯数字场景)——

def test_invoice_amount_extraction_cascade(monkeypatch, tmp_path):
    """正则抽空(纯数字无单位)+ 列指认也抽空 → 第三级 LLM 金额抽取成功,代码累加。

    模拟发票:表头乱码("日期","Money")、金额是纯数字("1680.00"/"640.00")无单位,
    正则与列指认都无法定位 → 进入第三级,LLM 抽取 + grounding 校验后代码求和。
    """
    pdf_path = _make_real_pdf(tmp_path, "a.pdf", num_pages=1)
    # content 字段提供 grounding 文本(两个金额都在其中)
    block = Block(
        block_id="p0-t0", page_index=0, label="table",
        content="日期 | Money\n2024 | 1680.00\n2025 | 640.00",
        table=TableStructure(
            headers=["日期", "Money"],
            rows=[["2024", "1680.00"], ["2025", "640.00"]],
        ),
    )
    reader = _make_mock_reader([[block]])
    _patch_get_ocr_engine(monkeypatch, reader)
    _patch_page_metas(monkeypatch, 1)

    import document_comparison.statement_pipeline as sp
    monkeypatch.setattr(sp, "render_page", lambda path, page_index, dpi=200: b"fake-png")

    # 列指认失败(表头无金额关键词)
    import document_comparison.statement.llm_column_detect as lcd
    monkeypatch.setattr(
        lcd, "llm_detect_amount_columns", lambda png, headers, **kwargs: None
    )
    # 第三级 LLM 金额抽取成功(模拟 grounding 通过的 items)
    from document_comparison.models import StatementAmountItem
    import document_comparison.statement.llm_amount_extract as lae

    def fake_extract(png, headers, rows, grounding, **kw):
        # 只在 grounding 含这些值时返回(模拟 grounding 校验已通过)
        assert "1680.00" in grounding
        assert "640.00" in grounding
        return [
            StatementAmountItem(value=1680.0, canonical="CNY:1680", column="金额(llm)"),
            StatementAmountItem(value=640.0, canonical="CNY:640", column="金额(llm)"),
        ]
    monkeypatch.setattr(lae, "llm_extract_amounts", fake_extract)

    report = run_statement_pipeline([pdf_path], ["a.pdf"])
    assert report.grand_total == 2320.0  # 1680 + 640
    assert report.verdict != "changed"
    assert report.column_detection_summary.get("llm") == 1
    assert report.files[0].total_amount == 2320.0


def test_invoice_realistic_header_amount_plain_digits(monkeypatch, tmp_path):
    """真实发票场景的回归:表头 OCR 出了"金额"列(启发式列定位成功),但单元格是
    纯数字无单位("1680.00"),正则抽不出 → 必须越过"列定位成功"继续触发第三级。

    这是 _has_amounts 判定修复的核心:旧逻辑用 if column_source 判断会在此
    直接 return(grand_total 永远 0),必须改用 column_sums/declared_totals 判定。
    """
    pdf_path = _make_real_pdf(tmp_path, "a.pdf", num_pages=1)
    block = Block(
        block_id="p0-t0", page_index=0, label="table",
        content="货物名称 | 金额\nA | 1680.00\nB | 640.00",
        table=TableStructure(
            headers=["货物名称", "金额"],  # 表头命中启发式 amount
            rows=[["A", "1680.00"], ["B", "640.00"]],  # 但纯数字无单位
        ),
    )
    reader = _make_mock_reader([[block]])
    _patch_get_ocr_engine(monkeypatch, reader)
    _patch_page_metas(monkeypatch, 1)

    import document_comparison.statement_pipeline as sp
    monkeypatch.setattr(sp, "render_page", lambda path, page_index, dpi=200: b"fake-png")

    # 第三级 LLM 金额抽取必须被调用;断言被调用且金额正确
    from document_comparison.models import StatementAmountItem
    import document_comparison.statement.llm_amount_extract as lae
    extract_called = {"count": 0}

    def fake_extract(png, headers, rows, grounding, **kw):
        extract_called["count"] += 1
        return [
            StatementAmountItem(value=1680.0, canonical="CNY:1680", column="金额"),
            StatementAmountItem(value=640.0, canonical="CNY:640", column="金额"),
        ]
    monkeypatch.setattr(lae, "llm_extract_amounts", fake_extract)

    report = run_statement_pipeline([pdf_path], ["a.pdf"])
    # 关键:第三级被触发了(若用旧 column_source 判断,count 会是 0)
    assert extract_called["count"] == 1
    assert report.grand_total == 2320.0


def test_invoice_amount_extraction_all_fail(monkeypatch, tmp_path):
    """正则 + 列指认 + 第三级 LLM 金额抽取全失败 → needs_review。"""
    pdf_path = _make_real_pdf(tmp_path, "a.pdf", num_pages=1)
    block = _make_table_block(
        headers=["日期", "Money"],
        rows=[["2024", "1680.00"]],
        page_index=0,
    )
    reader = _make_mock_reader([[block]])
    _patch_get_ocr_engine(monkeypatch, reader)
    _patch_page_metas(monkeypatch, 1)

    import document_comparison.statement_pipeline as sp
    monkeypatch.setattr(sp, "render_page", lambda path, page_index, dpi=200: b"fake-png")

    import document_comparison.statement.llm_column_detect as lcd
    monkeypatch.setattr(
        lcd, "llm_detect_amount_columns", lambda png, headers, **kwargs: None
    )
    import document_comparison.statement.llm_amount_extract as lae
    monkeypatch.setattr(lae, "llm_extract_amounts", lambda *a, **k: None)

    report = run_statement_pipeline([pdf_path], ["a.pdf"])
    assert report.grand_total == 0.0
    assert report.verdict == "needs_review"
    assert report.column_detection_summary.get("none", 0) >= 1


def test_whole_page_fallback_when_no_table_block(monkeypatch, tmp_path):
    """OCR 没把发票解析成结构化表格(label="text" 而非 table),但文本里含金额
    → 整页文本兜底:把整页 OCR 文本喂给 LLM 抽取金额。

    图片型发票的典型场景:OCR 输出纯文本块,无 TableStructure。
    """
    pdf_path = _make_real_pdf(tmp_path, "a.pdf", num_pages=1)
    # OCR 只产出一个 text block(没有 table 结构),content 里含金额
    text_block = Block(
        block_id="p0-b0", page_index=0, label="text",
        content="发票\n货物名称 金额\nA 1680.00\nB 640.00\n价税合计 2320.00",
    )
    reader = _make_mock_reader([[text_block]])
    _patch_get_ocr_engine(monkeypatch, reader)
    _patch_page_metas(monkeypatch, 1)

    import document_comparison.statement_pipeline as sp
    monkeypatch.setattr(sp, "render_page", lambda path, page_index, dpi=200: b"fake-png")

    # 整页兜底调用 llm_extract_amounts(此时 merged_tables 为空,不经过 _summarize_one_table)
    from document_comparison.models import StatementAmountItem
    import document_comparison.statement.llm_amount_extract as lae
    extract_called = {"count": 0}

    def fake_extract(png, headers, rows, grounding, **kw):
        extract_called["count"] += 1
        # grounding 是整页文本,金额都在其中
        assert "1680.00" in grounding
        return [
            StatementAmountItem(value=1680.0, canonical="CNY:1680", column="金额(llm)"),
            StatementAmountItem(value=640.0, canonical="CNY:640", column="金额(llm)"),
        ]
    monkeypatch.setattr(lae, "llm_extract_amounts", fake_extract)

    report = run_statement_pipeline([pdf_path], ["a.pdf"])
    assert extract_called["count"] == 1  # 整页兜底被触发
    assert report.grand_total == 2320.0
    assert report.column_detection_summary.get("llm") == 1


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
