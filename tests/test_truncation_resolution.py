"""resolve_truncated_pdf:回收件页数截取判定(截取基准与放弃条件)。"""
from pathlib import Path
from xml.etree import ElementTree
import zipfile

import pytest

try:
    import pymupdf as fitz
except ImportError:
    import fitz  # type: ignore

from docx import Document  # type: ignore[import-untyped]

from document_comparison.models import TruncationRecord
from document_comparison.pipeline import resolve_truncated_pdf
from document_comparison.parsing.word import estimate_page_count


def _make_pdf(tmp_path: Path, name: str, n_pages: int) -> Path:
    path = tmp_path / name
    doc = fitz.open()
    for i in range(n_pages):
        page = doc.new_page(width=595, height=842)
        page.insert_text((72, 100), f"PAGE-{i}", fontname="helv", fontsize=24)
    doc.save(path)
    doc.close()
    return path


def _make_docx_without_page_evidence(tmp_path: Path) -> Path:
    """无有效保存页数(app.xml Pages=0)且无分页标记的文档。"""
    path = tmp_path / "source.docx"
    Document().save(path)
    with zipfile.ZipFile(path) as archive:
        members = {
            info.filename: archive.read(info.filename)
            for info in archive.infolist()
        }
    root = ElementTree.fromstring(members["docProps/app.xml"])
    pages = root.find("{http://schemas.openxmlformats.org/officeDocument/2006/extended-properties}Pages")
    assert pages is not None
    pages.text = "0"
    members["docProps/app.xml"] = ElementTree.tostring(
        root, encoding="utf-8", xml_declaration=True
    )
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in members.items():
            archive.writestr(name, content)
    return path


def test_explicit_count_slices_longer_target(tmp_path: Path):
    word = _make_docx_without_page_evidence(tmp_path)
    target = _make_pdf(tmp_path, "target.pdf", 5)
    out = tmp_path / "compared.pdf"

    sliced, record = resolve_truncated_pdf(
        word, target, original_page_count=3, output_path=out
    )

    assert sliced == out
    assert record == TruncationRecord(
        original_pdf_page_count=5,
        truncated_pdf_page_count=3,
        original_doc_page_count=3,
        doc_page_count_source="explicit",
    )


def test_count_ge_target_pages_keeps_original(tmp_path: Path):
    word = _make_docx_without_page_evidence(tmp_path)
    target = _make_pdf(tmp_path, "target.pdf", 3)
    for original in (3, 10):
        sliced, record = resolve_truncated_pdf(
            word, target, original_page_count=original, output_path=None
        )
        assert sliced == target
        assert record is None


def test_unavailable_estimate_skips_truncation(tmp_path: Path):
    """DOCX 毫无分页证据(估算为 0)时不得按臆测页数截取。"""
    word = _make_docx_without_page_evidence(tmp_path)
    assert estimate_page_count(word) == 0
    target = _make_pdf(tmp_path, "target.pdf", 4)

    sliced, record = resolve_truncated_pdf(
        word, target, original_page_count=None, output_path=None
    )

    assert sliced == target
    assert record is None


def test_pdf_source_uses_real_page_count(tmp_path: Path):
    word = _make_pdf(tmp_path, "source.pdf", 3)
    target = _make_pdf(tmp_path, "target.pdf", 5)
    out = tmp_path / "compared.pdf"

    sliced, record = resolve_truncated_pdf(
        word, target, original_page_count=None, output_path=out
    )

    assert sliced == out
    assert record is not None
    assert record.truncated_pdf_page_count == 3


def test_invalid_explicit_count_rejected(tmp_path: Path):
    word = _make_docx_without_page_evidence(tmp_path)
    target = _make_pdf(tmp_path, "target.pdf", 2)
    with pytest.raises(ValueError):
        resolve_truncated_pdf(word, target, original_page_count=0, output_path=None)
