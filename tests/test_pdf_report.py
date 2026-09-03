"""PDF 比对报告渲染(pymupdf 拼版)+ 外部产物落盘单元测试。"""
from datetime import datetime, timezone

import pymupdf

from document_comparison.models import (
    Diff,
    DiffSegment,
    PageRecognitionDiagnostic,
    PageRegion,
    TamperReport,
)
from document_comparison.report.pdf_report import render_pdf_report


def _modified_diff():
    return Diff(
        alignment_id="a-1",
        status="modified",
        segments=[
            DiffSegment(op="equal", text="金额为"),
            DiffSegment(op="delete", text="100"),
            DiffSegment(op="insert", text="200"),
            DiffSegment(op="equal", text="万元。"),
        ],
        number="第三条",
        title="金额",
    )


def _tiny_png(path, label: str) -> None:
    doc = pymupdf.open()
    page = doc.new_page(width=300, height=420)
    page.insert_text((30, 60), label, fontname="china-s", fontsize=12)
    page.get_pixmap(dpi=72).save(str(path))
    doc.close()


def _span_colors(pdf_path) -> dict[str, str]:
    """收集 (文本 → 十六进制颜色),用于断言差异片段着色。"""
    colors: dict[str, str] = {}
    doc = pymupdf.open(str(pdf_path))
    for page in doc:
        for block in page.get_text("dict")["blocks"]:
            for line in block.get("lines", []):
                for span in line["spans"]:
                    text = span["text"].strip()
                    if text:
                        colors.setdefault(text, f"#{span['color']:06x}")
    doc.close()
    return colors


def test_render_pdf_report_basic_fields(tmp_path):
    report = TamperReport(
        source="s.docx", target="t.pdf", change_status="changed", diffs=[_modified_diff()]
    )
    out = tmp_path / "report.pdf"
    # render_pdf_report 不做时区转换;调用方(write_external_pdf_report)负责传入北京时间。
    render_pdf_report("BILL-001", report, datetime(2026, 8, 7, 15, 30), out)

    assert out.is_file()
    doc = pymupdf.open(str(out))
    text = "".join(page.get_text() for page in doc)
    assert "合同比对报告" in text
    assert "BILL-001" in text
    assert "发现确认内容变化" in text  # change_status→中文
    assert "2026-08-07 15:30" in text
    assert "差异数量:1" in text
    assert "第三条 金额" in text
    doc.close()


def test_render_pdf_report_modified_segments_colored(tmp_path):
    """delete 片段红色、insert 片段绿色(与 HTML 报告 del/ins 着色同口径)。"""
    report = TamperReport(
        source="s.docx", target="t.pdf", change_status="changed", diffs=[_modified_diff()]
    )
    out = tmp_path / "report.pdf"
    render_pdf_report("BILL-001", report, datetime(2026, 8, 7, 15, 30), out)
    colors = _span_colors(out)
    assert colors.get("100", "").startswith("#") and colors["100"] != "#212121"
    # 红色系(删除)
    r, g, b = (int(colors["100"][i : i + 2], 16) for i in (1, 3, 5))
    assert r > g and r > b
    # 绿色系(插入)
    r2, g2, b2 = (int(colors["200"][i : i + 2], 16) for i in (1, 3, 5))
    assert g2 > r2 and g2 > b2


def test_render_pdf_report_empty_diffs_shows_placeholder(tmp_path):
    report = TamperReport(source="s", target="t", change_status="clean", diffs=[])
    out = tmp_path / "report.pdf"
    render_pdf_report("BILL-002", report, datetime(2026, 8, 7, 15, 30), out)
    doc = pymupdf.open(str(out))
    text = "".join(page.get_text() for page in doc)
    assert "未发现内容变化" in text
    doc.close()


def test_pdf_report_explains_seal_review_in_business_language(tmp_path):
    report = TamperReport(
        source="s.pdf",
        target="t.pdf",
        change_status="needs_review",
        recognition_status="needs_review",
        recognition_diagnostics=[PageRecognitionDiagnostic(
            page_index=1,
            source="fallback",
            reliable=False,
            reasons=["检测到印章，覆盖区二次 OCR 与原图结果不一致"],
        )],
    )
    out = tmp_path / "seal-review.pdf"

    render_pdf_report("BILL-SEAL", report, datetime.now(timezone.utc), out)

    doc = pymupdf.open(str(out))
    text = "".join(page.get_text() for page in doc)
    assert "请人工核对" in text
    assert "第2页：检测到印章遮挡" in text
    assert "两次识别结果无法相互确认" in text
    assert "未识别到可确认的内容变化" in text
    assert "覆盖区二次 OCR 与原图结果不一致" not in text
    doc.close()


def test_render_pdf_report_includes_unmatched_clauses(tmp_path):
    added = Diff(
        alignment_id="u-1",
        status="added",
        segments=[DiffSegment(op="insert", text="新增条款")],
    )
    report = TamperReport(
        source="s", target="t", change_status="changed", diffs=[], unmatched_clauses=[added]
    )
    out = tmp_path / "report.pdf"
    render_pdf_report("BILL-003", report, datetime.now(timezone.utc), out)
    doc = pymupdf.open(str(out))
    text = "".join(page.get_text() for page in doc)
    assert "新增条款" in text
    assert "差异数量:1" in text  # diffs + unmatched_clauses 合计
    doc.close()


def test_render_pdf_report_page_hints_from_real_regions(tmp_path):
    """表头页码提示与 HTML 同口径:只输出真实报告坐标得出的页码。"""
    located = _modified_diff()
    located.page_regions = [PageRegion(page_index=2, bbox=[0.1, 0.1, 0.2, 0.2])]
    located.source_page_regions = [PageRegion(page_index=1, bbox=[0.1, 0.1, 0.2, 0.2])]
    report = TamperReport(source="s.pdf", target="t.pdf", change_status="changed", diffs=[located])
    out = tmp_path / "report.pdf"
    render_pdf_report("BILL-NAV", report, datetime.now(timezone.utc), out)
    doc = pymupdf.open(str(out))
    text = "".join(page.get_text() for page in doc)
    assert "采购部合同第2页" in text
    assert "供应商合同第3页" in text
    doc.close()


def test_render_pdf_report_appends_image_pages(tmp_path):
    """高亮标注 PNG 逐页拼入报告末尾,每页一个,带页码说明。"""
    report = TamperReport(source="s", target="t", change_status="clean", diffs=[])
    imgs = []
    for idx in (1, 2):
        p = tmp_path / f"hl-{idx}.png"
        _tiny_png(p, f"第{idx}页")
        imgs.append(p)
    source_img = tmp_path / "src-1.png"
    _tiny_png(source_img, "原件")

    out = tmp_path / "report.pdf"
    render_pdf_report(
        "BILL-IMG",
        report,
        datetime(2026, 8, 7, 15, 30),
        out,
        highlight_images=imgs,
        source_highlight_images=[source_img],
    )
    doc = pymupdf.open(str(out))
    text = "".join(page.get_text() for page in doc)
    assert "高亮标注图(采购部合同)" in text
    assert "高亮标注图(供应商合同)" in text
    assert "第 1 页 / 共 2 页" in text
    assert "第 2 页 / 共 2 页" in text
    # 内容页 1 页 + 原件 1 页 + 回收件 2 页
    assert len(doc) == 4
    # 末页应有图片
    assert doc[len(doc) - 1].get_images()
    doc.close()


def test_render_pdf_report_page_count_mismatch_note(tmp_path):
    """两侧高亮图页数不一致时,图片区头部给出说明;一致时不出现。"""
    report = TamperReport(source="s", target="t", change_status="clean", diffs=[])
    target_imgs = []
    for idx in (1, 2, 3):
        p = tmp_path / f"target-{idx}.png"
        _tiny_png(p, f"回收{idx}")
        target_imgs.append(p)
    source_imgs = []
    for idx in (1, 2):
        p = tmp_path / f"source-{idx}.png"
        _tiny_png(p, f"原件{idx}")
        source_imgs.append(p)

    out = tmp_path / "report-mismatch.pdf"
    render_pdf_report(
        "BILL-NOTE",
        report,
        datetime.now(timezone.utc),
        out,
        highlight_images=target_imgs,
        source_highlight_images=source_imgs,
    )
    doc = pymupdf.open(str(out))
    text = "".join(page.get_text() for page in doc)
    assert "两份文件分页不一致" in text
    assert "采购部合同共 2 页" in text
    assert "供应商合同共 3 页" in text
    doc.close()

    out_equal = tmp_path / "report-equal.pdf"
    render_pdf_report(
        "BILL-NONE",
        report,
        datetime.now(timezone.utc),
        out_equal,
        highlight_images=target_imgs[:2],
        source_highlight_images=source_imgs,
    )
    doc = pymupdf.open(str(out_equal))
    text = "".join(page.get_text() for page in doc)
    assert "两份文件分页不一致" not in text
    doc.close()


def test_render_pdf_report_long_text_flows_across_pages(tmp_path):
    """超长条款自动折行并跨页流动,不抛错、不丢尾部文本。"""
    long_text = "甲方应按本合同约定履行义务并承担相应责任," * 60
    diff = Diff(
        alignment_id="a-long",
        status="modified",
        segments=[
            DiffSegment(op="equal", text=long_text),
            DiffSegment(op="delete", text="尾部被删"),
            DiffSegment(op="insert", text="尾部新增"),
        ],
    )
    report = TamperReport(source="s", target="t", change_status="changed", diffs=[diff])
    out = tmp_path / "report.pdf"
    render_pdf_report("BILL-LONG", report, datetime.now(timezone.utc), out)
    doc = pymupdf.open(str(out))
    assert len(doc) >= 2
    text = "".join(page.get_text() for page in doc)
    assert "尾部被删" in text
    assert "尾部新增" in text
    doc.close()


# —— write_external_pdf_report 产物生成(tasks.py 调用入口)——


def test_write_external_pdf_report_writes_file_with_beijing_time(tmp_path, monkeypatch):
    """write_external_pdf_report 落盘到 reports_dir,头部时间为北京时间,并拼入 PNG 页。"""
    from document_comparison.config import settings
    from document_comparison.external_api import (
        external_images_dir,
        external_pdf_report_path,
        write_external_pdf_report,
    )

    monkeypatch.setattr(settings, "storage_dir", tmp_path / "storage")
    settings.ensure_dirs()
    task_id = "task-pdf-1"
    img_dir = external_images_dir(task_id)
    img_dir.mkdir(parents=True)
    _tiny_png(img_dir / "page-0001.png", "第1页")

    report = TamperReport(
        source="s.docx", target="t.pdf", change_status="changed", diffs=[_modified_diff()]
    )
    # 传 UTC finished_at;write 函数应转北京时间写入头部。
    path = write_external_pdf_report(
        "task-pdf-1", "BILL-PROD", report, datetime(2026, 8, 7, 7, 30, tzinfo=timezone.utc)
    )
    assert path == external_pdf_report_path(task_id)
    assert path.is_file()
    doc = pymupdf.open(str(path))
    text = "".join(page.get_text() for page in doc)
    assert "BILL-PROD" in text
    # UTC 07:30 → 北京 15:30
    assert "2026-08-07 15:30" in text
    # 图片目录 1 页 PNG → 报告含 1 个图片页
    assert any(page.get_images() for page in doc)
    doc.close()


def test_external_report_filename_pdf_suffix():
    from document_comparison.external_api import external_report_filename

    fn = external_report_filename(
        "BILL-001", datetime(2026, 8, 7, 7, 30, tzinfo=timezone.utc), suffix=".pdf"
    )
    assert fn == "【BILL-001】对比20260807-1530.pdf"
