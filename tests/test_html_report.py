"""HTML 比对报告渲染 + 外部下载文件名 helper 单元测试。"""
from datetime import datetime, timezone

from document_comparison.external_api import (
    external_report_filename,
    safe_filename_stem,
)
from document_comparison.models import Diff, DiffSegment, TamperReport
from document_comparison.report import render_html_report


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


def test_render_html_report_basic_fields():
    report = TamperReport(
        source="s.docx", target="t.pdf", change_status="changed", diffs=[_modified_diff()]
    )
    # render_html_report 不做时区转换;调用方(write_external_html_report)负责传入北京时间。
    generated = datetime(2026, 8, 7, 15, 30)  # 已是北京时间
    html = render_html_report("BILL-001", report, generated)

    assert "<!DOCTYPE html>" in html
    assert "单据号:</b>BILL-001" in html
    assert "发现确认内容变化" in html  # change_status→中文
    assert "生成时间:</b>2026-08-07 15:30" in html
    assert "差异数量:</b>1" in html


def test_render_html_report_modified_segments_highlighted():
    report = TamperReport(
        source="s.docx", target="t.pdf", change_status="changed", diffs=[_modified_diff()]
    )
    html = render_html_report("BILL-001", report, datetime.now(timezone.utc))
    # delete 片段红底、insert 片段绿底
    assert '<span class="del">100</span>' in html
    assert '<span class="ins">200</span>' in html
    # equal 片段不出现在着色 span 里
    assert '<span class="del">金额为' not in html


def test_render_html_report_escapes_html_special_chars():
    diff = Diff(
        alignment_id="a-1",
        status="added",
        segments=[DiffSegment(op="insert", text="<script>x</script>")],
    )
    report = TamperReport(source="s", target="t", change_status="changed", diffs=[diff])
    html = render_html_report("<X>", report, datetime.now(timezone.utc))
    assert "<script>x</script>" not in html  # 原文不可裸出现
    assert "&lt;script&gt;" in html
    assert "&lt;X&gt;" in html


def test_render_html_report_empty_diffs_shows_placeholder():
    report = TamperReport(source="s", target="t", change_status="clean", diffs=[])
    html = render_html_report("BILL-002", report, datetime.now(timezone.utc))
    assert "未发现内容变化" in html


def test_render_html_report_includes_unmatched_clauses():
    added = Diff(alignment_id="u-1", status="added", segments=[DiffSegment(op="insert", text="新增条款")])
    report = TamperReport(
        source="s", target="t", change_status="changed", diffs=[], unmatched_clauses=[added]
    )
    html = render_html_report("BILL-003", report, datetime.now(timezone.utc))
    assert "新增条款" in html
    assert "差异数量:</b>1" in html  # diffs + unmatched_clauses 合计


# —— 文件名 helper ——

def test_external_report_filename_beijing_time():
    # UTC 2026-08-07 07:30 → 北京 15:30
    fn = external_report_filename("BILL-001", datetime(2026, 8, 7, 7, 30, tzinfo=timezone.utc))
    assert fn == "【BILL-001】对比20260807-1530.html"


def test_external_report_filename_none_time_falls_back_to_now():
    fn = external_report_filename("BILL-001", None)
    assert fn.startswith("【BILL-001】对比")
    assert fn.endswith(".html")
    # 形如 YYYYMMDD-HHMM(11 字符时间戳)
    assert len(fn) == len("【BILL-001】对比20260807-1530.html")


def test_external_report_filename_sanitizes_unsafe_chars():
    fn = external_report_filename('a/b:c?d', datetime(2026, 1, 2, 3, 4, tzinfo=timezone.utc))
    # 危险字符替换为 _;北京时间为 11:04
    assert fn == "【a_b_c_d】对比20260102-1104.html"


def test_safe_filename_stem_edge_cases():
    assert safe_filename_stem("") == "report"
    assert safe_filename_stem("   ") == "report"
    # 路径分隔符替换;首尾点/空格被剥
    assert "/" not in safe_filename_stem("../x/../")
    assert "\\" not in safe_filename_stem("a\\b")
    # 超长截断到 80
    assert len(safe_filename_stem("x" * 200)) == 80


# —— write_external_html_report 产物生成(tasks.py 调用入口)——


def test_write_external_html_report_writes_file_with_beijing_time(tmp_path, monkeypatch):
    """write_external_html_report 落盘到 reports_dir,头部时间为北京时间。"""
    from document_comparison.config import settings
    from document_comparison.external_api import (
        external_html_report_path,
        write_external_html_report,
    )

    monkeypatch.setattr(settings, "storage_dir", tmp_path / "storage")
    settings.ensure_dirs()
    report = TamperReport(
        source="s.docx", target="t.pdf", change_status="changed", diffs=[_modified_diff()]
    )
    # 传 UTC finished_at;write 函数应转北京时间写入头部。
    path = write_external_html_report(
        "task-prod-1", "BILL-PROD", report, datetime(2026, 8, 7, 7, 30, tzinfo=timezone.utc)
    )
    assert path == external_html_report_path("task-prod-1")
    assert path.is_file()
    content = path.read_text(encoding="utf-8")
    assert "BILL-PROD" in content
    # UTC 07:30 → 北京 15:30
    assert "2026-08-07 15:30" in content



def test_render_html_report_with_highlight_images():
    """highlight_images 非空时,渲染按页内嵌的图片区。"""
    report = TamperReport(source="s", target="t", change_status="clean", diffs=[])
    html = render_html_report(
        "BILL-IMG",
        report,
        datetime(2026, 8, 7, 15, 30),
        highlight_images=["data:image/png;base64,AAA", "data:image/png;base64,BBB"],
    )
    assert "高亮标注图(供应商合同)" in html
    assert "第 1 页 / 共 2 页" in html
    assert "第 2 页 / 共 2 页" in html
    assert 'src="data:image/png;base64,AAA"' in html
    assert 'src="data:image/png;base64,BBB"' in html
    assert "max-width:none" in html


def test_render_html_report_includes_source_highlight_images():
    report = TamperReport(
        source="s.pdf", target="t.pdf", change_status="changed", diffs=[_modified_diff()]
    )
    html = render_html_report(
        "BILL-001",
        report,
        datetime.now(timezone.utc),
        highlight_images=["data:image/png;base64,TARGET"],
        source_highlight_images=["data:image/png;base64,SOURCE"],
    )
    assert "高亮标注图(采购部合同)" in html
    assert "base64,SOURCE" in html
    assert "高亮标注图(供应商合同)" in html
    assert 'class="bidirectional-pages"' in html
    assert 'id="source-pages"' in html
    assert 'id="target-pages"' in html
    assert "syncScroll" in html


def test_render_html_report_without_images_omits_section():
    """highlight_images 为空/None 时,不出现图片区标题。"""
    report = TamperReport(source="s", target="t", change_status="clean", diffs=[])
    html_none = render_html_report("B", report, datetime.now(), highlight_images=None)
    html_empty = render_html_report("B", report, datetime.now(), highlight_images=[])
    assert "高亮标注图" not in html_none
    assert "高亮标注图" not in html_empty


def test_write_external_html_report_embeds_highlight_images(tmp_path, monkeypatch):
    """write 函数读取 {task_id}_images/page-*.png,按页序 base64 内嵌进 HTML。"""
    import base64

    from document_comparison.config import settings
    from document_comparison.external_api import (
        external_images_dir,
        write_external_html_report,
    )

    monkeypatch.setattr(settings, "storage_dir", tmp_path / "storage")
    settings.ensure_dirs()
    task_id = "task-img-1"
    img_dir = external_images_dir(task_id)
    img_dir.mkdir(parents=True)
    # 故意乱序命名,验证按页号排序
    p1, p2 = b"\x89PNG_fake1", b"\x89PNG_fake2"
    (img_dir / "page-0002.png").write_bytes(p2)
    (img_dir / "page-0001.png").write_bytes(p1)

    report = TamperReport(source="s", target="t", change_status="changed", diffs=[])
    path = write_external_html_report(task_id, "BILL-IMG-1", report)
    content = path.read_text(encoding="utf-8")

    assert "data:image/png;base64," in content
    assert base64.b64encode(p1).decode() in content
    assert base64.b64encode(p2).decode() in content
    assert "第 1 页 / 共 2 页" in content
    assert "第 2 页 / 共 2 页" in content
