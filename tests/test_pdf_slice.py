"""slice_pdf:回收件页数截取工具。"""
from pathlib import Path

try:
    import pymupdf as fitz
except ImportError:
    import fitz  # type: ignore

from document_comparison.parsing.pdf import count_pages, slice_pdf


def _make_pdf(tmp_path: Path, n_pages: int) -> Path:
    path = tmp_path / f"{n_pages}p.pdf"
    doc = fitz.open()
    for i in range(n_pages):
        page = doc.new_page(width=595, height=842)
        # 写入页码,便于验证截取后保留的是前 N 页
        page.insert_text((72, 100), f"PAGE-{i}", fontname="helv", fontsize=24)
    doc.save(path)
    doc.close()
    return path


def test_slice_truncates_to_n_pages(tmp_path: Path):
    src = _make_pdf(tmp_path, 5)
    out = slice_pdf(src, 3)
    assert out != src  # 生成了新文件
    assert count_pages(out) == 3
    # 内容限于前 3 页
    doc = fitz.open(str(out))
    try:
        texts = [doc[i].get_text() for i in range(len(doc))]
    finally:
        doc.close()
    assert all("PAGE-0" in texts[0] for _ in [0])
    assert "PAGE-0" in texts[0]
    assert "PAGE-2" in texts[2]
    assert "PAGE-3" not in "".join(texts)


def test_slice_no_op_when_n_pages_ge_total(tmp_path: Path):
    src = _make_pdf(tmp_path, 3)
    # 等于总页数:直接返回原路径
    assert slice_pdf(src, 3) == src
    # 大于总页数:同样直接返回原路径
    assert slice_pdf(src, 10) == src


def test_slice_zero_or_negative_noops(tmp_path: Path):
    src = _make_pdf(tmp_path, 2)
    # n_pages<=0 无意义,直接返回原路径(不生成空 PDF)
    assert slice_pdf(src, 0) == src
    assert slice_pdf(src, -3) == src
