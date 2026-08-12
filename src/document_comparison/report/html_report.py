"""合同比对 HTML 报告渲染:自包含单文件,内联样式,可直接浏览器打开。

与 ``external_api.build_result_text`` 口径一致(同一套文案映射与差异文本规则),
渲染为带样式的结构化 HTML,供外部系统下载归档。
"""
from __future__ import annotations

import html
from datetime import datetime

from ..models import Diff, DiffSegment, TamperReport

# —— 文案映射(与 external_api.build_result_text 保持一致)——
_CONCLUSION = {
    "changed": "发现确认内容变化",
    "needs_review": "存在待人工复核内容",
    "clean": "未发现内容变化",
}
_RECOGNITION = {"reliable": "可靠", "needs_review": "待人工复核"}
_LOCATION = {"complete": "完整", "partial": "部分缺失", "missing": "缺失"}
_STATUS_NAMES = {
    "modified": "修改",
    "added": "新增",
    "deleted": "删除",
    "identical": "一致",
}


def _diff_texts(diff: Diff) -> tuple[str, str]:
    """与 external_api._diff_texts 同款:equal+delete=原始,equal+insert=回收。"""
    original = "".join(
        segment.text for segment in diff.segments if segment.op in {"equal", "delete"}
    )
    recovered = "".join(
        segment.text for segment in diff.segments if segment.op in {"equal", "insert"}
    )
    return original, recovered


def _render_segmented(original_html: list[str], recovered_html: list[str], diff: Diff) -> None:
    """对 modified 差异,按 segment op 着色高亮(delete 红/insert 绿)。

    非修改状态(added/deleted/identical)直接用拼接纯文本,不着色。
    original/ecovered 列表就地追加,调用方再 join。
    """
    if diff.status != "modified":
        original, recovered = _diff_texts(diff)
        original_html.append(html.escape(original))
        recovered_html.append(html.escape(recovered))
        return
    for seg in diff.segments:
        text = html.escape(seg.text)
        if seg.op == "delete":
            original_html.append(f'<span class="del">{text}</span>')
        elif seg.op == "insert":
            recovered_html.append(f'<span class="ins">{text}</span>')
        else:  # equal
            original_html.append(text)
            recovered_html.append(text)
    # 若某侧无着色片段(modified 但 segments 退化),退化为纯文本,避免空列。
    if not original_html:
        original, _ = _diff_texts(diff)
        original_html.append(html.escape(original))
    if not recovered_html:
        _, recovered = _diff_texts(diff)
        recovered_html.append(html.escape(recovered))


def _label(diff: Diff) -> str:
    value = " ".join(v for v in (diff.number, diff.title) if v).strip()
    return value or diff.alignment_id


_CSS = """
body{font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;max-width:none;margin:24px;padding:0;color:#222;background:#f7f7f9}
h1{font-size:20px;border-bottom:2px solid #2c7be5;padding-bottom:8px;margin-bottom:4px}
.meta{display:flex;flex-wrap:wrap;gap:8px 24px;margin:14px 0;font-size:14px}
.meta b{color:#444}
.badge{display:inline-block;padding:2px 10px;border-radius:10px;font-size:12px;color:#fff}
.badge-modified{background:#e8590c}.badge-added{background:#1971c2}.badge-deleted{background:#e03131}.badge-identical{background:#868e96}
table{width:100%;border-collapse:collapse;background:#fff;box-shadow:0 1px 3px rgba(0,0,0,.06);font-size:13px}
th,td{border:1px solid #e3e3e5;padding:8px 10px;vertical-align:top;text-align:left}
th{background:#f1f3f5;white-space:nowrap}
tbody tr[data-target-page],tbody tr[data-source-page]{cursor:pointer}
tbody tr[data-target-page]:hover,tbody tr[data-source-page]:hover{background:#eef6ff}
tbody tr.report-row-selected{outline:2px solid #2c7be5;outline-offset:-2px;background:#eef6ff}
.idx{width:36px;color:#888;text-align:center}
.col-status{width:64px}
.del{background:#ffe3e3;text-decoration:line-through;border-radius:2px;padding:0 1px}
.ins{background:#d3f9d3;border-radius:2px;padding:0 1px}
.empty{color:#adb5bd}
.summary{margin:10px 0 6px;color:#555;font-size:13px}
footer{margin-top:20px;color:#adb5bd;font-size:12px;text-align:center}
.pages-title{font-size:16px;margin:24px 0 10px;border-left:4px solid #2c7be5;padding-left:8px}
.pages{display:flex;flex-direction:column;gap:16px}
.pages figure{margin:0;background:#fff;border:1px solid #e3e3e5;border-radius:4px;padding:8px;box-shadow:0 1px 3px rgba(0,0,0,.06)}
.pages figcaption{font-size:12px;color:#868e96;margin-bottom:6px}
.pages img{display:block;width:100%;height:auto;border:1px solid #f1f3f5}
.bidirectional-pages{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px;margin-top:24px}
.comparison-pane{min-width:0;background:#fff;border:1px solid #e3e3e5;border-radius:4px;padding:10px;box-shadow:0 1px 3px rgba(0,0,0,.06)}
.comparison-pane h2{font-size:16px;margin:0 0 10px;border-left:4px solid #2c7be5;padding-left:8px}
.comparison-pane-scroll{max-height:min(78vh,900px);overflow-y:auto;overscroll-behavior:contain;padding-right:4px}
@media (max-width:900px){body{margin:10px}.bidirectional-pages{gap:6px}.comparison-pane{padding:4px}.comparison-pane h2{font-size:13px;margin-bottom:6px;border-left-width:3px;padding-left:5px}.comparison-pane-scroll{max-height:65vh;padding-right:0}.pages{gap:6px}.pages figure{padding:3px}.pages figcaption{font-size:10px;margin-bottom:3px}}
@media print{body{background:#fff}table,.comparison-pane{box-shadow:none}.pages figure{box-shadow:none;break-inside:avoid}.bidirectional-pages{display:block}.comparison-pane{margin-top:16px}.comparison-pane-scroll{max-height:none;overflow:visible}}
"""

_BIDIRECTIONAL_SCROLL_SCRIPT = """
<script>
(() => {
  const source = document.getElementById('source-pages');
  const target = document.getElementById('target-pages');
  let syncing = false;
  const syncScroll = (from, to) => {
    if (!from || !to) return;
    if (syncing) return;
    const fromMax = from.scrollHeight - from.clientHeight;
    const toMax = to.scrollHeight - to.clientHeight;
    if (fromMax <= 0 || toMax <= 0) return;
    syncing = true;
    to.scrollTop = (from.scrollTop / fromMax) * toMax;
    requestAnimationFrame(() => { syncing = false; });
  };
  if (source && target) {
    source.addEventListener('scroll', () => syncScroll(source, target), { passive: true });
    target.addEventListener('scroll', () => syncScroll(target, source), { passive: true });
  }

  const rows = document.querySelectorAll('tr[data-target-page], tr[data-source-page]');
  const jump = (row) => {
    const scrollToPage = (container, prefix, page) => {
      if (!page) return;
      const figure = document.getElementById(`${prefix}-page-${page}`);
      if (!figure) return;
      if (container) {
        container.scrollTo({
          top: figure.getBoundingClientRect().top - container.getBoundingClientRect().top + container.scrollTop,
          behavior: 'smooth',
        });
      } else {
        figure.scrollIntoView({ behavior: 'smooth', block: 'start' });
      }
    };
    rows.forEach((item) => item.classList.remove('report-row-selected'));
    row.classList.add('report-row-selected');
    scrollToPage(source, 'source', row.dataset.sourcePage);
    scrollToPage(target, 'target', row.dataset.targetPage);
  };
  rows.forEach((row) => {
    row.tabIndex = 0;
    row.addEventListener('click', () => jump(row));
    row.addEventListener('keydown', (event) => {
      if (event.key === 'Enter' || event.key === ' ') {
        event.preventDefault();
        jump(row);
      }
    });
  });
})();
</script>
"""


def render_html_report(
    document_no: str,
    report: TamperReport,
    generated_at: datetime,
    highlight_images: list[str] | None = None,
    source_highlight_images: list[str] | None = None,
) -> str:
    """生成自包含 HTML 报告字符串。

    ``generated_at`` 用于头部「生成时间」展示(调用方负责转北京时间)。
    ``highlight_images`` 与 ``source_highlight_images`` 分别为供应商合同、采购部合同的
    每页高亮标注图 data URI(或可访问 URL)列表，非空时在差异表格下方按页内嵌。
    """
    diffs = [*report.diffs, *report.unmatched_clauses]
    conclusion = _CONCLUSION.get(report.change_status, report.change_status)
    recognition = _RECOGNITION.get(report.recognition_status, report.recognition_status)
    location = _LOCATION.get(report.location_status, report.location_status)
    stamp = generated_at.strftime("%Y-%m-%d %H:%M")

    rows: list[str] = []
    empty_cell = '<span class="empty">（无）</span>'
    for index, diff in enumerate(diffs, start=1):
        original_parts: list[str] = []
        recovered_parts: list[str] = []
        _render_segmented(original_parts, recovered_parts, diff)
        status_zh = _STATUS_NAMES.get(diff.status, diff.status)
        original_cell = "".join(original_parts) or empty_cell
        recovered_cell = "".join(recovered_parts) or empty_cell
        label_cell = html.escape(_label(diff))
        status_cls = html.escape(diff.status)
        status_text = html.escape(status_zh)
        # 页码始终由真实报告坐标得出；没有坐标时不输出 data 属性，避免把
        # 无法定位的差异伪装成可以跳到精确高亮的位置。
        target_page = next((region.page_index + 1 for region in diff.page_regions), None)
        source_page = next(
            (region.page_index + 1 for region in diff.source_page_regions), None
        )
        row_attrs = ""
        if target_page is not None:
            row_attrs += f' data-target-page="{target_page}"'
        if source_page is not None:
            row_attrs += f' data-source-page="{source_page}"'
        rows.append(
            f"<tr{row_attrs}>"
            f'<td class="idx">{index}</td>'
            f'<td class="col-status"><span class="badge badge-{status_cls}">{status_text}</span></td>'
            f"<td>{label_cell}</td>"
            f"<td>{original_cell}</td>"
            f"<td>{recovered_cell}</td>"
            "</tr>"
        )

    rows_html = "\n".join(rows) if rows else (
        '<tr><td colspan="5" class="empty" style="text-align:center">未发现内容变化</td></tr>'
    )

    def _page_figures(images: list[str], side: str, dom_prefix: str) -> str:
        figures = "\n".join(
            f'<figure id="{dom_prefix}-page-{idx}"><figcaption>第 {idx} 页 / 共 {len(images)} 页</figcaption>'
            f'<img alt="{side}高亮标注 第{idx}页" src="{src}"></figure>'
            for idx, src in enumerate(images, start=1)
        )
        return figures

    def _render_images(title: str, images: list[str] | None) -> str:
        if not images:
            return ""
        return (
            f'<h2 class="pages-title">高亮标注图({title})</h2>'
            f'<div class="pages">{_page_figures(images, title, "source" if title == "采购部合同" else "target")}</div>'
        )

    # 两侧都可用时以独立可滚动面板左右展示，按相对滚动距离同步。
    # 若某一侧没有定位产物，仍以单侧纵向报告输出，避免出现空白对照栏。
    if source_highlight_images and highlight_images:
        images_html = (
            '<div class="bidirectional-pages">'
            '<section class="comparison-pane"><h2>高亮标注图(采购部合同)</h2>'
            f'<div id="source-pages" class="comparison-pane-scroll pages">{_page_figures(source_highlight_images, "采购部合同", "source")}</div>'
            '</section>'
            '<section class="comparison-pane"><h2>高亮标注图(供应商合同)</h2>'
            f'<div id="target-pages" class="comparison-pane-scroll pages">{_page_figures(highlight_images, "供应商合同", "target")}</div>'
            '</section></div>'
        )
        scroll_script = _BIDIRECTIONAL_SCROLL_SCRIPT
    else:
        images_html = _render_images("采购部合同", source_highlight_images) + _render_images(
            "供应商合同", highlight_images
        )
        # 单侧高亮图也允许差异行跳到本侧页面；脚本会自动跳过不存在的另一侧。
        scroll_script = _BIDIRECTIONAL_SCROLL_SCRIPT if (source_highlight_images or highlight_images) else ""

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>合同比对报告 · {html.escape(document_no)}</title>
<style>{_CSS}</style>
</head>
<body>
<h1>合同比对报告</h1>
<div class="meta">
  <div><b>单据号:</b>{html.escape(document_no)}</div>
  <div><b>结论:</b>{html.escape(conclusion)}</div>
  <div><b>识别状态:</b>{html.escape(recognition)}</div>
  <div><b>高亮定位:</b>{html.escape(location)}</div>
  <div><b>差异数量:</b>{len(diffs)}</div>
  <div><b>生成时间:</b>{html.escape(stamp)}</div>
</div>
<table>
<thead><tr><th class="idx">#</th><th class="col-status">状态</th><th>条款</th><th>采购部合同</th><th>供应商合同</th></tr></thead>
<tbody>
{rows_html}
</tbody>
</table>
{images_html}
<footer>本报告由合同篡改检测系统自动生成</footer>
{scroll_script}
</body>
</html>
"""
