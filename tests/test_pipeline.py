"""端到端管线测试:Word(docx)→ PDF → 比对。"""
import io
from pathlib import Path

from docx import Document  # type: ignore[import-untyped]

try:
    import pymupdf as fitz
except ImportError:
    import fitz  # type: ignore

from document_comparison.embed.mock import MockEmbedding
from document_comparison.models import Block, PageMeta
from document_comparison.parsing.pdf import extract_text_blocks
from document_comparison.pipeline import run_pipeline
from document_comparison.report.builder import burn_pdf
from document_comparison.report.docx_burn import burn_docx


class _TextLayerOCR:
    """用 PDF 文本层充当 mock OCR(避开真实 LLM 调用,测试稳定可复现)。"""

    def recognize(self, pdf_path: Path, page_metas: list[PageMeta], *, on_progress=None):
        return extract_text_blocks(pdf_path)


class _MergedTextLayerOCR:
    """模拟原生 PDF 把多个逻辑条款合并在同一文本块的情况。"""

    def recognize(self, pdf_path: Path, page_metas: list[PageMeta], *, on_progress=None):
        return [[
            Block(
                block_id="merged",
                page_index=0,
                label="text",
                bbox=[72, 72, 520, 180],
                content=(
                    "第三条费用及支付方式 3.1 总额为100元。"
                    "3.2 支付50%预付款。"
                ),
            )
        ]]


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
    report = run_pipeline(wpath, ppath, enable_risk_assessment=True)
    assert report.overall_risk == "high"
    # 金额变化应体现在 key_elements
    assert any(e.kind == "amount" and e.changed for e in report.key_elements)


def test_pipeline_risk_disabled_by_default(tmp_path):
    """默认 enable_risk_assessment=False:同样的金额篡改,差异仍被报告,
    但不做风险分级、不抽取 key_elements,overall 镜像 change_status(=changed,不再 low)。"""
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
    # 不传 enable_risk_assessment(用默认 False)
    report = run_pipeline(wpath, ppath)

    # 差异仍被识别
    assert len(report.diffs) >= 1
    assert all(d.status == "modified" for d in report.diffs)
    # 风险字段被短路
    assert all(d.risk_level == "none" for d in report.diffs)
    assert all(d.risk_reasons == [] for d in report.diffs)
    # 高风险要素未抽取
    assert report.key_elements == []
    # overall 镜像 change_status:有差异 → changed(不再出现 low/medium/high)
    assert report.change_status == "changed"
    assert report.overall_risk == "changed"


def test_pipeline_header_field_alignment_no_false_positive(tmp_path):
    """首部键值块按字段名配对,不应误报 added/deleted(注入 mock OCR/embed,不依赖网络)。"""
    parts = [
        ("p", "甲方(甲方主体):XX公司"),
        ("p", "联系地址:上海市浦东新区"),
        ("p", "联系电话:13800138000"),
        ("p", "乙方(乙方主体):YY公司"),
        ("p", "联系地址:上海市黄浦区"),
        ("p", "联系电话:13900139000"),
        ("h1", "第一条 合同标的"),
        ("p", "甲方提供设备。"),
    ]
    word_buf = _make_word(parts)
    # PDF 文本层内容与 Word 完全一致(逐行)
    pdf_buf = _make_pdf_from_lines([
        "甲方(甲方主体):XX公司",
        "联系地址:上海市浦东新区",
        "联系电话:13800138000",
        "乙方(乙方主体):YY公司",
        "联系地址:上海市黄浦区",
        "联系电话:13900139000",
        "第一条 合同标的",
        "甲方提供设备。",
    ])
    wpath, ppath = _to_files(word_buf, pdf_buf, tmp_path)
    report = run_pipeline(wpath, ppath, ocr=_TextLayerOCR(), embed=MockEmbedding())
    # 首部字段全部配对成功,无 added/deleted 误报
    assert len(report.unmatched_clauses) == 0, (
        f"expected no unmatched, got: {[d.status for d in report.unmatched_clauses]}"
    )
    assert report.overall_risk == "clean"


def test_pipeline_recovers_inline_pdf_boundaries_and_marks_only_real_change(tmp_path):
    """块内条款合并时仍应配对成功，并只报告真实比例变化。"""
    word_buf = _make_word([
        ("h1", "第三条 费用及支付方式"),
        ("p", "3.1 总额为100元。"),
        ("p", "3.2 支付60%预付款。"),
    ])
    # 让 PDF 侧 3.2 变为 50%，同时模拟章节、3.1、3.2 在同一原生 block。
    pdf_buf = _make_pdf_from_lines(["placeholder"])
    wpath, ppath = _to_files(word_buf, pdf_buf, tmp_path)
    progress_events: list[tuple[str, float]] = []
    report = run_pipeline(
        wpath,
        ppath,
        ocr=_MergedTextLayerOCR(),
        embed=MockEmbedding(),
        on_progress=lambda stage, progress: progress_events.append((stage, progress)),
        enable_risk_assessment=True,
    )

    assert not report.unmatched_clauses
    assert [diff.number for diff in report.diffs] == ["3.2"]
    assert any(element.kind == "ratio" for element in report.key_elements)
    assert [stage for stage, _ in progress_events if stage in {"structure", "align", "compare"}] == [
        "structure", "align", "compare",
    ]

    annotated_pdf = tmp_path / "annotated.pdf"
    annotated_docx = tmp_path / "annotated.docx"
    burn_pdf(ppath, report, annotated_pdf)
    burn_docx(wpath, report, annotated_docx)
    assert annotated_pdf.exists() and annotated_pdf.stat().st_size > 0
    assert annotated_docx.exists() and annotated_docx.stat().st_size > 0
