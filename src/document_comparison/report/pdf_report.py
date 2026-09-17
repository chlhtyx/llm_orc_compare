"""合同比对 PDF 报告渲染:pymupdf 直接拼版,自包含单文件,零新增依赖。

与 ``html_report.render_html_report`` 同一套文案映射与差异文本规则
(equal+delete=原始、equal+insert=回收),内容为:概要 + 差异明细 + 逐页高亮标注图。
中文统一使用 pymupdf 内置 ``china-s`` 字体(与 ``builder.burn_pdf`` 标签同源),
无需宿主字体环境,离线可渲染。
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pymupdf

from ..models import Diff, TamperReport
from .page_scope_notice import page_scope_notice
from .review_notice import empty_diff_message, review_notice_intro, review_notice_messages

# 富文本片段:(文本, 颜色, 删除线)。
_Run = tuple[str, tuple[float, float, float], bool]
_Runs = list[_Run]

# —— 文案映射(与 html_report / external_api.build_result_text 保持一致)——
_CONCLUSION = {
    "changed": "发现确认内容变化",
    "needs_review": "存在待人工复核内容",
    "clean": "未发现内容变化",
}
_CONCLUSION_COLOR = {
    "changed": (0.75, 0.12, 0.12),
    "needs_review": (0.85, 0.47, 0.05),
    "clean": (0.08, 0.52, 0.28),
}
_RECOGNITION = {"reliable": "可靠", "needs_review": "待人工复核"}
_LOCATION = {"complete": "完整", "partial": "部分缺失", "missing": "缺失"}
_STATUS_NAMES = {
    "modified": "修改",
    "added": "新增",
    "deleted": "删除",
    "identical": "一致",
}
# 状态徽章底色(与 HTML badge 同色系:e8590c/1971c2/e03131/868e96)。
_STATUS_BADGE = {
    "modified": (0.91, 0.35, 0.05),
    "added": (0.10, 0.44, 0.76),
    "deleted": (0.88, 0.19, 0.19),
    "identical": (0.53, 0.56, 0.59),
}

_INK = (0.13, 0.13, 0.13)
_MUTED = (0.45, 0.48, 0.51)
_NOTE = (0.54, 0.35, 0.0)  # 说明行(与 HTML pages-note #8a5a00 同色)
_DEL = (0.78, 0.12, 0.12)
_INS = (0.08, 0.47, 0.21)
_ACCENT = (0.17, 0.48, 0.90)
_BADGE_TEXT = (1.0, 1.0, 1.0)

_FONT = "china-s"
_PAGE_W, _PAGE_H = 595.0, 842.0  # A4(pt)
_MARGIN = 46.0
_FOOTER_RESERVE = 26.0


def _diff_texts(diff: Diff) -> tuple[str, str]:
    """与 external_api._diff_texts 同款:equal+delete=原始,equal+insert=回收。"""
    original = "".join(
        segment.text for segment in diff.segments if segment.op in {"equal", "delete"}
    )
    recovered = "".join(
        segment.text for segment in diff.segments if segment.op in {"equal", "insert"}
    )
    return original, recovered


def _label(diff: Diff) -> str:
    value = " ".join(v for v in (diff.number, diff.title) if v).strip()
    return value or diff.alignment_id


def _char_width(ch: str, fontsize: float) -> float:
    return pymupdf.get_text_length(ch, fontname=_FONT, fontsize=fontsize)


def _text_width(text: str, fontsize: float) -> float:
    return sum(_char_width(ch, fontsize) for ch in text)


def _wrap_runs(runs: _Runs, max_width: float, fontsize: float) -> list[_Runs]:
    """逐字贪心折行(CJK 任意位置可断),返回每行的 (text,color,strike) 片段。"""
    lines: list[_Runs] = []
    line: _Runs = []
    width = 0.0

    def _flush() -> None:
        nonlocal line, width
        # 相邻同色同删除线片段合并,减少 insert_text 次数。
        merged: _Runs = []
        for text, color, strike in line:
            if merged and merged[-1][1] == color and merged[-1][2] == strike:
                merged[-1] = (merged[-1][0] + text, color, strike)
            else:
                merged.append((text, color, strike))
        lines.append(merged)
        line = []
        width = 0.0

    for text, color, strike in runs:
        for ch in text:
            if ch == "\n":
                _flush()
                continue
            cw = _char_width(ch, fontsize)
            if line and width + cw > max_width:
                _flush()
            line.append((ch, color, strike))
            width += cw
    if line:
        _flush()
    return lines


class _Layout:
    """游标式排版器:逐行落墨,页满自动换页;差异明细与图片区共用。"""

    def __init__(self, doc: pymupdf.Document):
        self.doc = doc
        self.page: pymupdf.Page | None = None
        self.y = _MARGIN
        self.content_w = _PAGE_W - _MARGIN * 2

    def ensure(self, needed: float) -> pymupdf.Page:
        bottom = _PAGE_H - _MARGIN - _FOOTER_RESERVE
        if self.page is None or self.y + needed > bottom:
            self.page = self.doc.new_page(width=_PAGE_W, height=_PAGE_H)
            self.y = _MARGIN
        return self.page

    def gap(self, height: float) -> None:
        self.y += height

    def draw_runs(
        self,
        runs: _Runs,
        *,
        fontsize: float = 9.5,
        x: float = _MARGIN,
        max_width: float | None = None,
        leading: float = 1.55,
    ) -> None:
        """绘制富文本段落,支持跨页流动。"""
        width = max_width if max_width is not None else self.content_w - (x - _MARGIN)
        for line in _wrap_runs(runs, width, fontsize):
            line_h = fontsize * leading
            page = self.ensure(line_h)
            baseline = self.y + fontsize * 1.02
            cursor_x = x
            for text, color, strike in line:
                page.insert_text(
                    (cursor_x, baseline),
                    text,
                    fontname=_FONT,
                    fontsize=fontsize,
                    color=color,
                )
                text_w = _text_width(text, fontsize)
                if strike:
                    page.draw_line(
                        (cursor_x, baseline - fontsize * 0.28),
                        (cursor_x + text_w, baseline - fontsize * 0.28),
                        color=color,
                        width=0.6,
                    )
                cursor_x += text_w
            self.y += line_h


def _meta_line(layout: _Layout, label: str, value: str, *, value_color=_INK) -> None:
    layout.draw_runs(
        [(f"{label}:", _MUTED, False), (value, value_color, False)],
        fontsize=10.5,
        leading=1.9,
    )


def _diff_block(layout: _Layout, index: int, diff: Diff) -> None:
    """单个差异块:表头条(序号+徽章+条款+页码提示)+ 原始/回收两段富文本。"""
    status_zh = _STATUS_NAMES.get(diff.status, diff.status)
    badge_color = _STATUS_BADGE.get(diff.status, _STATUS_BADGE["modified"])

    # 表头条:浅灰底 + 左侧状态色条;条款过长时换行加高。
    header_runs: _Runs = [(f"{index}  ", _MUTED, False), (_label(diff), _INK, False)]
    # 页码提示与 HTML 行跳转同口径:只输出真实报告坐标得出的页码。
    target_page = next((region.page_index + 1 for region in diff.page_regions), None)
    source_page = next(
        (region.page_index + 1 for region in diff.source_page_regions), None
    )
    hints = []
    if source_page is not None:
        hints.append(f"采购部合同第{source_page}页")
    if target_page is not None:
        hints.append(f"供应商合同第{target_page}页")
    if hints:
        header_runs.append((f"({';'.join(hints)})", _MUTED, False))

    header_fs = 10.5
    badge_w = _text_width(status_zh, 9) + 10
    header_lines = _wrap_runs(
        header_runs, layout.content_w - 16 - badge_w, header_fs
    )
    header_h = 10 + len(header_lines) * (header_fs * 1.35)
    page = layout.ensure(header_h)
    header_rect = pymupdf.Rect(_MARGIN, layout.y, _MARGIN + layout.content_w, layout.y + header_h)
    page.draw_rect(header_rect, color=None, fill=(0.945, 0.955, 0.965))
    page.draw_rect(
        pymupdf.Rect(_MARGIN, layout.y, _MARGIN + 3.2, layout.y + header_h),
        color=None,
        fill=badge_color,
    )
    # 徽章:状态色小块 + 白字。
    badge_rect = pymupdf.Rect(
        _MARGIN + 10,
        layout.y + header_h / 2 - 7,
        _MARGIN + 10 + badge_w,
        layout.y + header_h / 2 + 7,
    )
    page.draw_rect(badge_rect, color=None, fill=badge_color)
    page.insert_text(
        (badge_rect.x0 + 5, badge_rect.y1 - 4.4),
        status_zh,
        fontname=_FONT,
        fontsize=9,
        color=_BADGE_TEXT,
    )
    text_x = _MARGIN + 16 + badge_w
    for line_no, line in enumerate(header_lines):
        page.insert_text(
            (text_x, layout.y + 5 + (line_no + 1) * header_fs * 1.15),
            "".join(text for text, _c, _s in line),
            fontname=_FONT,
            fontsize=header_fs,
            color=_INK,
        )
    layout.y += header_h + 6

    body_x = _MARGIN + 12
    body_w = layout.content_w - 12
    original_runs = _original_runs(diff)
    recovered_runs = _recovered_runs(diff)
    layout.draw_runs([("采购部合同:", _MUTED, False)], fontsize=9, x=body_x)
    if original_runs:
        layout.draw_runs(original_runs, fontsize=9.5, x=body_x, max_width=body_w, leading=1.6)
    else:
        layout.draw_runs([("（无）", _MUTED, False)], fontsize=9.5, x=body_x, leading=1.6)
    layout.gap(2)
    layout.draw_runs([("供应商合同:", _MUTED, False)], fontsize=9, x=body_x)
    if recovered_runs:
        layout.draw_runs(recovered_runs, fontsize=9.5, x=body_x, max_width=body_w, leading=1.6)
    else:
        layout.draw_runs([("（无）", _MUTED, False)], fontsize=9.5, x=body_x, leading=1.6)
    layout.gap(6)


def _original_runs(diff: Diff) -> _Runs:
    """采购部合同侧:modified 时 delete 片段红色加删除线,其余正常。"""
    if diff.status != "modified":
        original, _ = _diff_texts(diff)
        return [(original, _INK, False)] if original else []
    return _segment_runs(diff, keep="delete")


def _recovered_runs(diff: Diff) -> _Runs:
    """供应商合同侧:modified 时 insert 片段绿色,其余正常。"""
    if diff.status != "modified":
        _, recovered = _diff_texts(diff)
        return [(recovered, _INK, False)] if recovered else []
    return _segment_runs(diff, keep="insert")


def _segment_runs(diff: Diff, *, keep: str) -> _Runs:
    runs: _Runs = []
    for seg in diff.segments:
        if seg.op == "equal":
            runs.append((seg.text, _INK, False))
        elif seg.op == keep:
            runs.append((seg.text, _DEL if keep == "delete" else _INS, keep == "delete"))
    return runs


def _section_title(layout: _Layout, title: str) -> None:
    layout.gap(8)
    page = layout.ensure(24)
    page.draw_rect(
        pymupdf.Rect(_MARGIN, layout.y + 2, _MARGIN + 4, layout.y + 18),
        color=None,
        fill=_ACCENT,
    )
    page.insert_text(
        (_MARGIN + 12, layout.y + 15),
        title,
        fontname=_FONT,
        fontsize=13,
        color=_INK,
    )
    layout.y += 26


def _review_notice_block(layout: _Layout, report: TamperReport) -> None:
    """在 PDF 概要后输出业务可读的人工核对事项。"""
    intro = review_notice_intro(report)
    if not intro:
        return
    _section_title(layout, "请人工核对")
    layout.draw_runs([(intro, _NOTE, False)], fontsize=10, leading=1.7)
    for index, message in enumerate(review_notice_messages(report), start=1):
        layout.draw_runs([(f"{index}. {message}", _INK, False)], fontsize=10, leading=1.7)
        layout.gap(2)


def _image_pages(
    doc: pymupdf.Document,
    title: str,
    images: list[Path],
    note: str | None = None,
) -> None:
    """高亮标注图区:每页 PNG 独占一个 PDF 页,标题 + 页码说明在图上方。

    ``note`` 非空时在标题下方输出一行说明(如两侧页数不一致提示)。
    """
    page = doc.new_page(width=_PAGE_W, height=_PAGE_H)
    page.draw_rect(
        pymupdf.Rect(_MARGIN, _MARGIN + 2, _MARGIN + 4, _MARGIN + 18),
        color=None,
        fill=_ACCENT,
    )
    page.insert_text(
        (_MARGIN + 12, _MARGIN + 15),
        f"高亮标注图({title})",
        fontname=_FONT,
        fontsize=13,
        color=_INK,
    )
    caption_y = _MARGIN + 34
    if note:
        page.insert_text(
            (_MARGIN, caption_y),
            note,
            fontname=_FONT,
            fontsize=9,
            color=_NOTE,
        )
        caption_y += 14
    page.insert_text(
        (_MARGIN, caption_y),
        f"第 1 页 / 共 {len(images)} 页",
        fontname=_FONT,
        fontsize=9,
        color=_MUTED,
    )
    page.insert_image(
        pymupdf.Rect(
            _MARGIN, caption_y + 8, _PAGE_W - _MARGIN, _PAGE_H - _MARGIN - _FOOTER_RESERVE
        ),
        filename=str(images[0]),
        keep_proportion=True,
    )
    for idx, image in enumerate(images[1:], start=2):
        page = doc.new_page(width=_PAGE_W, height=_PAGE_H)
        page.insert_text(
            (_MARGIN, _MARGIN + 8),
            f"第 {idx} 页 / 共 {len(images)} 页",
            fontname=_FONT,
            fontsize=9,
            color=_MUTED,
        )
        page.insert_image(
            pymupdf.Rect(
                _MARGIN, _MARGIN + 16, _PAGE_W - _MARGIN, _PAGE_H - _MARGIN - _FOOTER_RESERVE
            ),
            filename=str(image),
            keep_proportion=True,
        )


def render_pdf_report(
    document_no: str,
    report: TamperReport,
    generated_at: datetime,
    output_path: str | Path,
    highlight_images: list[Path] | None = None,
    source_highlight_images: list[Path] | None = None,
) -> Path:
    """生成自包含 PDF 报告并落盘,返回写入路径。

    ``generated_at`` 用于头部「生成时间」展示(调用方负责转北京时间)。
    ``highlight_images`` / ``source_highlight_images`` 分别为供应商合同(回收件)、
    采购部合同(原件)的每页高亮标注 PNG 路径,按页序逐页拼入报告末尾。
    """
    diffs = [*report.diffs, *report.unmatched_clauses]
    conclusion = _CONCLUSION.get(report.change_status, report.change_status)
    conclusion_color = _CONCLUSION_COLOR.get(report.change_status, _INK)
    recognition = _RECOGNITION.get(report.recognition_status, report.recognition_status)
    location = _LOCATION.get(report.location_status, report.location_status)
    stamp = generated_at.strftime("%Y-%m-%d %H:%M")
    scope_notice = page_scope_notice(report)

    doc = pymupdf.open()
    layout = _Layout(doc)

    # —— 概要头 ——
    page = layout.ensure(60)
    page.insert_text(
        (_MARGIN, layout.y + 18), "合同比对报告", fontname=_FONT, fontsize=20, color=_INK
    )
    page.draw_rect(
        pymupdf.Rect(_MARGIN, layout.y + 26, _PAGE_W - _MARGIN, layout.y + 27.2),
        color=None,
        fill=_ACCENT,
    )
    layout.y += 40

    _meta_line(layout, "单据号", document_no)
    _meta_line(layout, "结论", conclusion, value_color=conclusion_color)
    _meta_line(layout, "识别状态", recognition)
    _meta_line(layout, "高亮定位", location)
    _meta_line(layout, "差异数量", str(len(diffs)))
    _meta_line(layout, "生成时间", stamp)
    if scope_notice:
        _meta_line(layout, "正文范围", scope_notice)
    _review_notice_block(layout, report)

    # —— 差异明细 ——
    _section_title(layout, "差异明细")
    if diffs:
        for index, diff in enumerate(diffs, start=1):
            _diff_block(layout, index, diff)
    else:
        layout.draw_runs(
            [(empty_diff_message(report), _MUTED, False)], fontsize=10.5, leading=2.2
        )

    layout.gap(10)
    tail = layout.ensure(14)
    tail.draw_line(
        (_MARGIN, layout.y),
        (_PAGE_W - _MARGIN, layout.y),
        color=(0.85, 0.86, 0.88),
        width=0.8,
    )
    layout.y += 14
    layout.draw_runs(
        [("本报告由合同篡改检测系统自动生成", _MUTED, False)],
        fontsize=8.5,
    )

    # —— 高亮标注图(原件在前,与 HTML 报告两侧面板顺序一致)——
    # 两侧页数来自两份不同的物理 PDF(DOCX 原件侧为 LibreOffice 派生渲染),
    # 分页不同属正常;在图片区头部明示,与 HTML 报告口径一致。
    page_note = None
    if (
        source_highlight_images
        and highlight_images
        and len(source_highlight_images) != len(highlight_images)
    ):
        page_note = (
            f"采购部合同共 {len(source_highlight_images)} 页、供应商合同共 "
            f"{len(highlight_images)} 页,两份文件分页不一致,页码按各自文档独立展示"
        )
    if source_highlight_images:
        _image_pages(doc, "采购部合同", list(source_highlight_images), note=page_note)
    if highlight_images:
        _image_pages(doc, "供应商合同", list(highlight_images))

    # —— 页脚页码(最终统一补写,覆盖含图片页的全部页面)——
    total = len(doc)
    for i in range(total):
        doc[i].insert_text(
            (_PAGE_W / 2 - 14, _PAGE_H - _MARGIN / 2 + 6),
            f"{i + 1} / {total}",
            fontname=_FONT,
            fontsize=8,
            color=_MUTED,
        )

    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(out), deflate=True, garbage=3)
    doc.close()
    return out
