"""无标注版(纯文本 difflib)流水线测试:Word → PDF → 行级比对。

用 PDF 文本层充当 mock OCR(避开真实 LLM),测试稳定可复现。
"""
import io
from pathlib import Path

from docx import Document  # type: ignore[import-untyped]

try:
    import pymupdf as fitz
except ImportError:
    import fitz  # type: ignore

from document_comparison.models import PageMeta
from document_comparison.parsing.pdf import extract_text_blocks
from document_comparison.raw_pipeline import run_raw_pipeline


class _TextLayerOCR:
    """用 PDF 文本层充当 mock OCR(同 test_pipeline)。"""

    def recognize(self, pdf_path: Path, page_metas: list[PageMeta], *, on_progress=None):
        return extract_text_blocks(pdf_path)


def _make_word(parts):
    doc = Document()
    for kind, text in parts:
        if kind == "h1":
            doc.add_heading(text, level=1)
        elif kind == "p":
            doc.add_paragraph(text)
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
    buf.seek(0)
    return buf


def _to_files(word_buf, pdf_buf, tmp_path):
    wpath = tmp_path / "c.docx"
    ppath = tmp_path / "c.pdf"
    wpath.write_bytes(word_buf.getvalue())
    ppath.write_bytes(pdf_buf.getvalue())
    word_buf.seek(0)
    return wpath, ppath


def test_raw_identical(tmp_path):
    parts = [
        ("h1", "第一条 合同标的"),
        ("p", "甲方提供成套设备。"),
        ("h1", "第二条 合同金额"),
        ("p", "金额为100万元。"),
    ]
    word_buf = _make_word(parts)
    pdf_buf = _make_pdf_from_lines([
        "第一条 合同标的",
        "甲方提供成套设备。",
        "第二条 合同金额",
        "金额为100万元。",
    ])
    wpath, ppath = _to_files(word_buf, pdf_buf, tmp_path)
    report = run_raw_pipeline(wpath, ppath, ocr=_TextLayerOCR())
    assert len(report.hunks) == 0
    assert report.stats["similarity"] >= 0.99
    assert report.stats["replaced"] == 0
    assert report.stats["inserted"] == 0
    assert report.stats["deleted"] == 0


def test_raw_amount_replace(tmp_path):
    """Word 100万 vs PDF 200万 → 一个 replace hunk + 字符级 char_segments。"""
    parts = [
        ("h1", "第二条 合同金额"),
        ("p", "金额为100万元。"),
    ]
    word_buf = _make_word(parts)
    pdf_buf = _make_pdf_from_lines([
        "第二条 合同金额",
        "金额为200万元。",
    ])
    wpath, ppath = _to_files(word_buf, pdf_buf, tmp_path)
    report = run_raw_pipeline(wpath, ppath, ocr=_TextLayerOCR())
    assert len(report.hunks) == 1
    h = report.hunks[0]
    assert h.tag == "replace"
    assert h.word_lines == ["金额为100万元。"]
    assert h.pdf_lines == ["金额为200万元。"]
    # 字符级 segments 应有 delete(1) + insert(2)
    assert any(s.op == "delete" and "1" in s.text for s in h.char_segments)
    assert any(s.op == "insert" and "2" in s.text for s in h.char_segments)


def test_raw_insert_and_delete(tmp_path):
    """PDF 多一行(insert)场景:difflib 可能把它并入 replace,关键是有差异 + 统计合理。"""
    parts = [
        ("p", "第一行"),
        ("p", "第二行"),
        ("p", "第三行"),
    ]
    word_buf = _make_word(parts)
    # PDF:第二行被替换为"改过的第二行",并多一行"新增行"
    pdf_buf = _make_pdf_from_lines([
        "第一行",
        "改过的第二行",
        "新增行",
        "第三行",
    ])
    wpath, ppath = _to_files(word_buf, pdf_buf, tmp_path)
    report = run_raw_pipeline(wpath, ppath, ocr=_TextLayerOCR())
    # 应检出差异(replace 或 insert)
    assert len(report.hunks) >= 1
    tags = {h.tag for h in report.hunks}
    assert tags & {"replace", "insert"}
    # PDF 侧总行数应多于 Word 侧(多了一行)
    assert report.stats["pdf_total_lines"] > report.stats["word_total_lines"]
    assert report.stats["similarity"] < 0.95


def test_raw_char_level_disabled(tmp_path):
    """char_level=False 时 replace hunk 的 char_segments 为空。"""
    parts = [("p", "金额为100万元。")]
    word_buf = _make_word(parts)
    pdf_buf = _make_pdf_from_lines(["金额为200万元。"])
    wpath, ppath = _to_files(word_buf, pdf_buf, tmp_path)
    report = run_raw_pipeline(wpath, ppath, ocr=_TextLayerOCR(), char_level=False)
    assert len(report.hunks) == 1
    assert report.hunks[0].char_segments == []


def test_raw_report_carries_full_text(tmp_path):
    """报告应携带标准化后的 Word/PDF 全文(供前端展开)。"""
    parts = [("p", "测试条款A")]
    word_buf = _make_word(parts)
    pdf_buf = _make_pdf_from_lines(["测试条款A"])
    wpath, ppath = _to_files(word_buf, pdf_buf, tmp_path)
    report = run_raw_pipeline(wpath, ppath, ocr=_TextLayerOCR())
    assert "测试条款A" in report.word_text
    assert "测试条款A" in report.pdf_text
