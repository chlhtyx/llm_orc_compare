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

from document_comparison.models import Block, PageMeta, PageRecognitionDiagnostic
from document_comparison.parsing.pdf import extract_text_blocks
from document_comparison.raw_pipeline import run_raw_pipeline
from document_comparison.structure.normalize import normalize_table_text


class _TextLayerOCR:
    """用 PDF 文本层充当 mock OCR(同 test_pipeline)。"""

    def recognize(self, pdf_path: Path, page_metas: list[PageMeta], *, on_progress=None):
        return extract_text_blocks(pdf_path)


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


# —— 表格规范化 ——


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


# —— 表格流水线:Word 表格 vs OCR 返回不同格式 ——


class _FixedBlockOCR:
    """固定返回预设 block 列表的 mock OCR(模拟 LLM 识别结果)。

    用于验证 raw_pipeline 对 label=table 的 block 套用表格规范化。
    """

    def __init__(self, blocks_per_page, diagnostics=None):
        self._blocks = blocks_per_page
        self.last_diagnostics = diagnostics or []

    def recognize(self, pdf_path: Path, page_metas: list[PageMeta], *, on_progress=None):
        return self._blocks


def _table_block(content, page_index=0):
    return Block(
        block_id=f"p{page_index}-b0",
        page_index=page_index,
        label="table",
        bbox=[0, 0, 100, 100],
        content=content,
    )


def _text_block(content, page_index=0):
    return Block(
        block_id=f"p{page_index}-b0",
        page_index=page_index,
        label="text",
        bbox=[0, 0, 100, 100],
        content=content,
    )


def test_raw_table_markdown_no_false_diff(tmp_path):
    """Word 表格 vs OCR 返回 Markdown 表格(带分隔行):规范化后无伪差异。"""
    rows = [["阶段", "比例", "金额"], ["预付款", "30%", "30000"], ["尾款", "70%", "70000"]]
    word_buf = _make_word([("table", rows)])
    # PDF 文件本身不影响(用空页占位),OCR 结果由 mock 决定
    pdf_buf = _make_pdf_from_lines(["占位"])
    wpath, ppath = _to_files(word_buf, pdf_buf, tmp_path)

    ocr_md = _FixedBlockOCR([[_table_block(
        "| 阶段 | 比例 | 金额 |\n|---|---|---|\n| 预付款 | 30% | 30000 |\n| 尾款 | 70% | 70000 |"
    )]])
    report = run_raw_pipeline(wpath, ppath, ocr=ocr_md)
    assert len(report.hunks) == 0
    assert report.stats["similarity"] >= 0.99


def test_raw_table_tsv_no_false_diff(tmp_path):
    """Word 表格 vs OCR 返回 TSV 制表符格式:规范化后无伪差异。"""
    rows = [["阶段", "比例", "金额"], ["预付款", "30%", "30000"], ["尾款", "70%", "70000"]]
    word_buf = _make_word([("table", rows)])
    pdf_buf = _make_pdf_from_lines(["占位"])
    wpath, ppath = _to_files(word_buf, pdf_buf, tmp_path)

    ocr_tsv = _FixedBlockOCR([[_table_block(
        "阶段\t比例\t金额\n预付款\t30%\t30000\n尾款\t70%\t70000"
    )]])
    report = run_raw_pipeline(wpath, ppath, ocr=ocr_tsv)
    assert len(report.hunks) == 0
    assert report.stats["similarity"] >= 0.99


def test_raw_table_real_change_detected(tmp_path):
    """表格内真实改动仍被检出:Word 30000 vs OCR 40000。"""
    rows = [["阶段", "比例", "金额"], ["预付款", "30%", "30000"], ["尾款", "70%", "70000"]]
    word_buf = _make_word([("table", rows)])
    pdf_buf = _make_pdf_from_lines(["占位"])
    wpath, ppath = _to_files(word_buf, pdf_buf, tmp_path)

    ocr_changed = _FixedBlockOCR([[_table_block(
        "阶段 | 比例 | 金额\n预付款 | 30% | 40000\n尾款 | 70% | 70000"
    )]])
    report = run_raw_pipeline(wpath, ppath, ocr=ocr_changed)
    # 应检出差异
    assert len(report.hunks) >= 1
    assert report.stats["similarity"] < 0.99
    # 差异应落在金额行:Word 侧含 30000,PDF 侧含 40000
    w_lines = [ln for h in report.hunks for ln in h.word_lines]
    p_lines = [ln for h in report.hunks for ln in h.pdf_lines]
    assert any("30000" in ln for ln in w_lines)
    assert any("40000" in ln for ln in p_lines)


# —— 识别质量门禁 ——


def test_raw_report_keeps_reliable_recognition_status(tmp_path):
    """逐页识别可靠时，无标注版保持正常比对状态。"""
    word_buf = _make_word([("p", "合同金额为100万元。")])
    pdf_buf = _make_pdf_from_lines(["合同金额为100万元。"])
    wpath, ppath = _to_files(word_buf, pdf_buf, tmp_path)
    diagnostic = PageRecognitionDiagnostic(
        page_index=0,
        source="native",
        reliable=True,
        char_count=12,
    )
    ocr = _FixedBlockOCR(
        [[_text_block("合同金额为100万元。")]],
        diagnostics=[diagnostic],
    )

    report = run_raw_pipeline(wpath, ppath, ocr=ocr)

    assert report.recognition_status == "reliable"
    assert report.recognition_diagnostics == [diagnostic]
    assert "recognition_quality" not in report.stats


def test_raw_report_marks_unreliable_recognition_for_review(tmp_path):
    """识别异常时仍保留文本差异，但明确标记为仅供人工复核。"""
    word_buf = _make_word([("p", "合同金额为100万元。")])
    pdf_buf = _make_pdf_from_lines(["占位"])
    wpath, ppath = _to_files(word_buf, pdf_buf, tmp_path)
    diagnostic = PageRecognitionDiagnostic(
        page_index=0,
        source="fallback",
        reliable=False,
        reasons=["OCR 返回内容为空或字符过少"],
        char_count=2,
    )
    ocr = _FixedBlockOCR(
        [[_text_block("金额为200万元。")]],
        diagnostics=[diagnostic],
    )

    report = run_raw_pipeline(wpath, ppath, ocr=ocr)

    assert report.hunks
    assert report.recognition_status == "needs_review"
    assert report.recognition_diagnostics == [diagnostic]
    assert report.stats["recognition_quality"] == "needs_review"
    assert report.stats["unreliable_pages"] == [1]
