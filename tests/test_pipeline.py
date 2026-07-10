"""端到端管线测试:Word(docx)→ PDF → 比对。"""
import io

from docx import Document  # type: ignore[import-untyped]

try:
    import pymupdf as fitz
except ImportError:
    import fitz  # type: ignore

from document_comparison.pipeline import run_pipeline


def _make_word(parts):
    doc = Document()
    for kind, text in parts:
        if kind == "h1":
            doc.add_heading(text, level=1)
        elif kind == "h2":
            doc.add_heading(text, level=2)
        elif kind == "p":
            doc.add_paragraph(text)
    buf = io.BytesIO()
    doc.save(buf)
    return buf


def _make_pdf_from_lines(lines):
    # 用内置 CJK 字体 china-s,确保中文写入文本层并可被读回
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


def test_pipeline_identical(tmp_path):
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
    report = run_pipeline(wpath, ppath)
    assert report.overall_risk == "clean"
    assert len(report.diffs) == 0


def test_pipeline_detects_amount_tamper(tmp_path):
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
    report = run_pipeline(wpath, ppath)
    assert report.overall_risk == "high"
    # 金额变化应体现在 key_elements
    assert any(e.kind == "amount" and e.changed for e in report.key_elements)
