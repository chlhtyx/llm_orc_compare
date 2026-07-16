"""无标注版(LLM 比对)流水线测试:Word → PDF 整篇提取 → LLM 差异比对。

新管线不再用 difflib:Word 展平为文本、PDF 整篇判定(原生优先/扫描件长图整体 OCR)
后 normalize,再由 LLM 单次调用产出结构化 TextDiffReport。
这里 mock 掉 LLM 比对(`llm_text_diff`),保证测试稳定可复现、不依赖外部服务。
"""
import io
from pathlib import Path
from unittest.mock import MagicMock

from docx import Document  # type: ignore[import-untyped]

try:
    import pymupdf as fitz
except ImportError:
    import fitz  # type: ignore

from document_comparison.models import DiffSegment, TextDiffHunk, TextDiffReport
from document_comparison.raw_pipeline import run_raw_pipeline
from document_comparison.structure.normalize import normalize_table_text
import document_comparison.raw_pipeline as raw_pipeline_mod
import document_comparison.ocr.whole_doc as whole_doc_mod


import pytest


@pytest.fixture(autouse=True)
def _low_native_threshold(monkeypatch):
    """测试用 PDF 文本量很小,把整篇文字层判定阈值降到 1,确保走原生路径。

    真实部署阈值在 whole_doc._MIN_NATIVE_CHARS(16),不影响生产行为。
    """
    monkeypatch.setattr(whole_doc_mod, "_MIN_NATIVE_CHARS", 1)


def _make_word(parts):
    """构造 Word 文档。parts 元素:
    - ("h1", text)  标题
    - ("p", text)   段落
    - ("table", rows)  rows 为二维 list,每行一组单元格
    """
    doc = Document()
    for kind, text in parts:
        if kind == "h1":
            doc.add_heading(text, level=1)
        elif kind == "p":
            doc.add_paragraph(text)
        elif kind == "table":
            rows = text
            n_cols = max(len(r) for r in rows)
            tbl = doc.add_table(rows=len(rows), cols=n_cols)
            tbl.style = "Table Grid"
            for r, row in enumerate(rows):
                for c, val in enumerate(row):
                    tbl.cell(r, c).text = val
    buf = io.BytesIO()
    doc.save(buf)
    return buf


def _make_pdf_from_lines(lines):
    pdf = fitz.open()
    page = pdf.new_page(width=595, height=842)
    y = 72
    for ln in lines:
        page.insert_text((72, y), ln, fontname="china-s", fontsize=11)
        y += 20
    buf = io.BytesIO()
    pdf.save(buf)
    pdf.close()
    buf.seek(0)
    return buf


def _to_files(word_buf, pdf_buf, tmp_path):
    wpath = tmp_path / "c.docx"
    ppath = tmp_path / "c.pdf"
    wpath.write_bytes(word_buf.getvalue())
    ppath.write_bytes(pdf_buf.getvalue())
    word_buf.seek(0)
    return wpath, ppath


def _mock_llm_report(hunks, similarity=1.0, word_text="", pdf_text=""):
    """构造一个 LLM 比对的返回报告(供 mock 使用)。"""
    return TextDiffReport(
        source="",
        target="",
        word_text=word_text,
        pdf_text=pdf_text,
        hunks=hunks,
        stats={
            "similarity": similarity,
            "equal_lines": 0,
            "replaced": sum(max(len(h.word_lines), len(h.pdf_lines)) for h in hunks if h.tag == "replace"),
            "deleted": sum(len(h.word_lines) for h in hunks if h.tag == "delete"),
            "inserted": sum(len(h.pdf_lines) for h in hunks if h.tag == "insert"),
            "engine": "llm",
        },
    )


def _patch_llm(monkeypatch, report):
    """把 raw_pipeline 调用的 llm_text_diff 替换为返回固定 report。"""
    monkeypatch.setattr(raw_pipeline_mod, "llm_text_diff", lambda *a, **k: report)


# —— PDF 整篇提取:原生文字层路径 ——


def test_raw_pdf_native_text_layer_used(tmp_path, monkeypatch):
    """带文字层的 PDF 应走原生提取(不调用 OCR 引擎 recognize_text)。"""
    parts = [("p", "测试条款A")]
    word_buf = _make_word(parts)
    pdf_buf = _make_pdf_from_lines(["测试条款A"])
    wpath, ppath = _to_files(word_buf, pdf_buf, tmp_path)

    captured = {}

    def fake_llm(word_text, pdf_text, **kw):
        captured["pdf_text"] = pdf_text
        return _mock_llm_report([], similarity=1.0)

    monkeypatch.setattr(raw_pipeline_mod, "llm_text_diff", fake_llm)
    report = run_raw_pipeline(wpath, ppath)

    # PDF 文本应来自原生文字层(包含「测试条款A」)
    assert "测试条款A" in captured["pdf_text"]
    assert len(report.hunks) == 0


def test_raw_identical(tmp_path, monkeypatch):
    """两端一致时 LLM 返回空 hunks,管线正常产出报告。"""
    parts = [("p", "金额为100万元。")]
    word_buf = _make_word(parts)
    pdf_buf = _make_pdf_from_lines(["金额为100万元。"])
    wpath, ppath = _to_files(word_buf, pdf_buf, tmp_path)
    _patch_llm(monkeypatch, _mock_llm_report([], similarity=1.0))

    report = run_raw_pipeline(wpath, ppath)
    assert len(report.hunks) == 0
    assert report.stats["similarity"] >= 0.99


def test_raw_amount_change(tmp_path, monkeypatch):
    """Word 100万 vs PDF 200万:LLM 返回 replace hunk + 字符级 segments。"""
    parts = [("p", "金额为100万元。")]
    word_buf = _make_word(parts)
    pdf_buf = _make_pdf_from_lines(["金额为200万元。"])
    wpath, ppath = _to_files(word_buf, pdf_buf, tmp_path)

    hunk = TextDiffHunk(
        tag="replace",
        word_lines=["金额为100万元。"],
        pdf_lines=["金额为200万元。"],
        char_segments=[
            DiffSegment(op="equal", text="金额为"),
            DiffSegment(op="delete", text="1"),
            DiffSegment(op="insert", text="2"),
            DiffSegment(op="equal", text="00万元。"),
        ],
        context_before=[],
        context_after=[],
    )
    _patch_llm(monkeypatch, _mock_llm_report([hunk], similarity=0.8))

    report = run_raw_pipeline(wpath, ppath)
    assert len(report.hunks) == 1
    h = report.hunks[0]
    assert h.tag == "replace"
    assert any(s.op == "delete" and "1" in s.text for s in h.char_segments)
    assert any(s.op == "insert" and "2" in s.text for s in h.char_segments)


def test_raw_char_level_flag_passed(tmp_path, monkeypatch):
    """char_level=False 时仍正常调用管线(char_level 透传给 LLM)。"""
    parts = [("p", "金额为100万元。")]
    word_buf = _make_word(parts)
    pdf_buf = _make_pdf_from_lines(["金额为200万元。"])
    wpath, ppath = _to_files(word_buf, pdf_buf, tmp_path)

    seen = {}

    def fake_llm(wt, pt, *, char_level, **kw):
        seen["char_level"] = char_level
        return _mock_llm_report([], similarity=1.0)

    monkeypatch.setattr(raw_pipeline_mod, "llm_text_diff", fake_llm)
    run_raw_pipeline(wpath, ppath, char_level=False)
    assert seen["char_level"] is False


def test_raw_report_carries_full_text(tmp_path, monkeypatch):
    """报告应携带标准化后的 Word/PDF 全文(供前端展开)。"""
    parts = [("p", "测试条款A")]
    word_buf = _make_word(parts)
    pdf_buf = _make_pdf_from_lines(["测试条款A"])
    wpath, ppath = _to_files(word_buf, pdf_buf, tmp_path)

    def fake_llm(word_text, pdf_text, **kw):
        return _mock_llm_report([], similarity=1.0, word_text=word_text, pdf_text=pdf_text)

    monkeypatch.setattr(raw_pipeline_mod, "llm_text_diff", fake_llm)
    report = run_raw_pipeline(wpath, ppath)
    assert "测试条款A" in report.word_text
    assert "测试条款A" in report.pdf_text


# —— PDF 整篇提取:扫描件逐页并发 OCR 路径 ——


def _make_scanned_pdf(pages: int = 1):
    """构造无可提取文字层的「扫描件」PDF(空白页,可多页)。"""
    pdf = fitz.open()
    for _ in range(pages):
        pdf.new_page(width=595, height=842)
    buf = io.BytesIO()
    pdf.save(buf)
    pdf.close()
    buf.seek(0)
    return buf


def test_raw_scanned_pdf_uses_per_page_ocr(tmp_path, monkeypatch):
    """无文字层的扫描件应走逐页并发 OCR(每页调一次 recognize_text)。"""
    parts = [("p", "第一条")]
    word_buf = _make_word(parts)
    pdf_buf = _make_scanned_pdf(pages=1)
    wpath, ppath = _to_files(word_buf, pdf_buf, tmp_path)

    mock_ocr = MagicMock()
    mock_ocr.recognize_text.return_value = "第一条"
    _patch_llm(monkeypatch, _mock_llm_report([], similarity=1.0))

    report = run_raw_pipeline(wpath, ppath, ocr=mock_ocr)

    # 单页扫描件 → recognize_text 调用一次
    assert mock_ocr.recognize_text.call_count == 1
    assert report.recognition_diagnostics


def test_raw_scanned_pdf_multi_page_concurrent(tmp_path, monkeypatch):
    """多页扫描件逐页并发 OCR,结果按页序拼接成整篇纯文本。"""
    parts = [("p", "第一条"), ("p", "第二条"), ("p", "第三条")]
    word_buf = _make_word(parts)
    pdf_buf = _make_scanned_pdf(pages=3)
    wpath, ppath = _to_files(word_buf, pdf_buf, tmp_path)

    # 每页返回不同文本,验证按页序拼接
    page_texts = ["第一条内容", "第二条内容", "第三条内容"]
    mock_ocr = MagicMock()
    mock_ocr.recognize_text.side_effect = list(page_texts)
    mock_ocr.max_concurrency = 2
    _patch_llm(monkeypatch, _mock_llm_report([], similarity=1.0))

    captured = {}

    def fake_llm(word_text, pdf_text, **kw):
        captured["pdf_text"] = pdf_text
        return _mock_llm_report([], similarity=1.0)

    monkeypatch.setattr(raw_pipeline_mod, "llm_text_diff", fake_llm)
    run_raw_pipeline(wpath, ppath, ocr=mock_ocr)

    # 3 页 → 调用 3 次;结果按页序拼接
    assert mock_ocr.recognize_text.call_count == 3
    joined = captured["pdf_text"]
    assert "第一条内容" in joined
    assert "第二条内容" in joined
    assert "第三条内容" in joined
    # 页序:第一条 在 第二条 之前
    assert joined.index("第一条内容") < joined.index("第三条内容")


def test_raw_scanned_pdf_empty_ocr_marked_needs_review(tmp_path, monkeypatch):
    """扫描件逐页 OCR 全部返回空文本 → 诊断不可靠 → needs_review。"""
    parts = [("p", "第一条")]
    word_buf = _make_word(parts)
    pdf_buf = _make_scanned_pdf()
    wpath, ppath = _to_files(word_buf, pdf_buf, tmp_path)

    mock_ocr = MagicMock()
    mock_ocr.recognize_text.return_value = ""  # OCR 啥也没识别到
    _patch_llm(monkeypatch, _mock_llm_report([], similarity=1.0))

    report = run_raw_pipeline(wpath, ppath, ocr=mock_ocr)
    assert report.recognition_status == "needs_review"
    assert report.stats["recognition_quality"] == "needs_review"


# —— 表格规范化(纯函数,与管线无关)——


def test_normalize_table_text_canonical_is_idempotent():
    """已是规范「cell | cell」格式时原样返回(幂等)。"""
    src = "阶段 | 比例 | 金额\n预付款 | 30% | 30000"
    assert normalize_table_text(src) == src


def test_normalize_table_text_strips_markdown_pipes():
    """Markdown 首尾包裹的 | 去掉,单元格仍按 | 分隔。"""
    md = "| 阶段 | 比例 | 金额 |\n| 预付款 | 30% | 30000 |"
    assert normalize_table_text(md) == "阶段 | 比例 | 金额\n预付款 | 30% | 30000"


def test_normalize_table_text_drops_separator_row():
    """Markdown 分隔行(|---|---|)被丢弃。"""
    md = "| 阶段 | 比例 | 金额 |\n|---|---|---|\n| 预付款 | 30% | 30000 |"
    out = normalize_table_text(md)
    assert "---" not in out
    assert out == "阶段 | 比例 | 金额\n预付款 | 30% | 30000"


def test_normalize_table_text_align_row_variants():
    """带对齐标记的分隔行(:---: / :---)同样丢弃。"""
    md = "阶段 | 比例 | 金额\n:---:|:---|---:\n预付款 | 30% | 30000"
    out = normalize_table_text(md)
    assert out == "阶段 | 比例 | 金额\n预付款 | 30% | 30000"


def test_normalize_table_text_tsv():
    """TSV 制表符分隔 → 规范「 | 」格式。"""
    tsv = "阶段\t比例\t金额\n预付款\t30%\t30000"
    assert normalize_table_text(tsv) == "阶段 | 比例 | 金额\n预付款 | 30% | 30000"


def test_normalize_table_text_collapses_cell_whitespace():
    """单元格内多空白折叠为单空格。"""
    src = "a   b |  c  d"
    assert normalize_table_text(src) == "a b | c d"
