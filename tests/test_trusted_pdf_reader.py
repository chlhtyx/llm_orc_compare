from __future__ import annotations

from pathlib import Path

import pymupdf

from document_comparison.models import (
    Block,
    Diff,
    PageMeta,
    PageRegion,
    PageRecognitionDiagnostic,
    TamperReport,
)
from document_comparison.ocr.native import NativePDFEngine, _canonicalize_table_rows
from document_comparison.ocr.quality import apply_recognition_gate
from document_comparison.ocr.trusted import TrustedPDFReader
from document_comparison.parsing.pdf import get_page_metas


def _make_table_pdf(path: Path) -> None:
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((54, 60), "Contract No: A-001", fontsize=11)

    xs = [50, 110, 260, 390]
    ys = [100, 130, 170, 210]
    for x in xs:
        page.draw_line((x, ys[0]), (x, ys[-1]))
    for y in ys:
        page.draw_line((xs[0], y), (xs[-1], y))

    rows = [
        ["No", "Code", "Name"],
        ["1", "YA0300001530A", "Braided sleeve 2mm"],
        ["2", "YA0300001531A", "Braided sleeve 12mm"],
    ]
    for row_index, row in enumerate(rows):
        for col_index, value in enumerate(row):
            page.insert_text(
                (xs[col_index] + 4, ys[row_index] + 20), value, fontsize=8
            )
    doc.save(path)
    doc.close()


def _make_mixed_pdf(path: Path) -> None:
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text(
        (54, 72),
        "1. Native text page contains enough reliable contract content.",
        fontsize=11,
    )
    doc.new_page(width=595, height=842)  # 模拟无文本层扫描页
    doc.save(path)
    doc.close()


def test_native_reader_recovers_table_without_duplicate_cell_text(tmp_path: Path):
    pdf_path = tmp_path / "table.pdf"
    _make_table_pdf(pdf_path)

    engine = NativePDFEngine()
    pages = engine.recognize(pdf_path, get_page_metas(pdf_path))

    tables = [block for block in pages[0] if block.label == "table"]
    assert len(tables) == 1
    assert tables[0].table is not None
    assert tables[0].table.headers == ["No", "Code", "Name"]
    assert tables[0].table.rows[0][1] == "YA0300001530A"
    assert tables[0].content.count("YA0300001530A") == 1
    assert all(
        "YA0300001530A" not in block.content
        for block in pages[0]
        if block.label != "table"
    )


def test_native_table_compacts_merged_summary_cells():
    rows = _canonicalize_table_rows([
        ["名称", "数量", "金额", "备注"],
        ["A", "2", "100", ""],
        ["含税总计", "", "", "300元"],
    ])

    assert rows == [
        ["名称", "数量", "金额", "备注"],
        ["A", "2", "100"],
        ["含税总计", "300元"],
    ]


class _FallbackOCR:
    def __init__(self) -> None:
        self.page_counts: list[int] = []

    def recognize(
        self,
        pdf_path: Path,
        page_metas: list[PageMeta],
        *,
        on_progress=None,
    ) -> list[list[Block]]:
        self.page_counts.append(len(page_metas))
        return [[
            Block(
                block_id="fallback-0",
                page_index=0,
                label="text",
                bbox=[10, 10, 200, 30],
                content="2. OCR text recovered from scanned page.",
            )
        ]]


def test_trusted_reader_sends_only_non_native_pages_to_fallback(tmp_path: Path):
    pdf_path = tmp_path / "mixed.pdf"
    _make_mixed_pdf(pdf_path)
    fallback = _FallbackOCR()
    engine = TrustedPDFReader(fallback)

    pages = engine.recognize(pdf_path, get_page_metas(pdf_path))

    assert fallback.page_counts == [1]
    assert "Native text page" in pages[0][0].content
    assert pages[1][0].page_index == 1
    assert "OCR text recovered" in pages[1][0].content
    assert [item.source for item in engine.last_diagnostics] == ["native", "fallback"]
    assert all(item.reliable for item in engine.last_diagnostics)
    assert all(item.location_status == "complete" for item in engine.last_diagnostics)


def test_fallback_diagnostic_tracks_location_without_changing_recognition_quality():
    from document_comparison.ocr.trusted import assess_fallback_page

    diagnostic = assess_fallback_page(
        [
            Block(
                block_id="located",
                page_index=0,
                label="text",
                bbox=[10, 10, 100, 30],
                content="已定位文字",
            ),
            Block(
                block_id="missing",
                page_index=0,
                label="text",
                bbox=[],
                content="这一段文字没有坐标但识别内容仍然可靠",
            ),
        ],
        0,
    )

    assert diagnostic.reliable is True
    assert diagnostic.location_status == "partial"
    assert 0 < diagnostic.bbox_coverage < 1


def test_unreliable_recognition_marks_only_related_diff_for_review():
    report = TamperReport(
        source="source.docx",
        target="target.pdf",
        overall_risk="high",
        change_status="changed",
        diffs=[
            Diff(
                alignment_id="al1",
                status="modified",
                risk_level="high",
                risk_reasons=["金额发生变化"],
                page_regions=[PageRegion(page_index=0, bbox=[0, 0, 1, 1])],
            ),
            Diff(
                alignment_id="al2",
                status="modified",
                risk_level="high",
                risk_reasons=["账号发生变化"],
                page_regions=[PageRegion(page_index=1, bbox=[0, 0, 1, 1])],
            ),
        ],
    )
    diagnostics = [
        PageRecognitionDiagnostic(
            page_index=0,
            source="native",
            reliable=True,
            char_count=120,
        ),
        PageRecognitionDiagnostic(
            page_index=1,
            source="fallback",
            reliable=False,
            reasons=["OCR 返回连续重复内容"],
            char_count=120,
        )
    ]

    apply_recognition_gate(report, diagnostics, enable_risk_assessment=True)

    assert report.overall_risk == "high"
    assert report.change_status == "changed"
    assert report.recognition_status == "needs_review"
    assert report.diffs[0].verdict == "changed"
    assert report.diffs[0].confidence == "high"
    assert report.diffs[0].risk_reasons == ["金额发生变化"]
    assert report.diffs[1].verdict == "needs_review"
    assert report.diffs[1].confidence == "low"
    assert report.diffs[1].risk_level == "high"
    assert "识别质量不足" in report.diffs[1].risk_reasons[-1]


def test_unreliable_recognition_prevents_empty_report_from_being_clean():
    report = TamperReport(
        source="source.docx",
        target="target.pdf",
        overall_risk="clean",
        change_status="clean",
    )
    diagnostics = [
        PageRecognitionDiagnostic(
            page_index=0,
            source="fallback",
            reliable=False,
            reasons=["OCR 返回内容为空或字符过少"],
            char_count=2,
        )
    ]

    apply_recognition_gate(report, diagnostics)

    assert report.overall_risk == "needs_review"
    assert report.change_status == "needs_review"
