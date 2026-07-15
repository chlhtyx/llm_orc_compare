"""将差异高亮烧录到源 docx：整段黄底标注被篡改条款位置。

PDF 扫描件没有文本层时（PaddleOCR 纯文本模式 / 扫描件无坐标），无法在 PDF 上画框，
但 docx 永远有文本层。这里把报告里的 modified / deleted 条款定位回 docx 原段落，
加段落底纹（w:shd）做整段高亮——只标「这里有改动」的位置，不逐字区分删/增，
因此不动任何 run，原字体/字号/颜色完全保留，兼容各种 docx 样式。

设计要点（兼容性）：
- 段落定位不依赖段落索引（不同 docx 段落数不同），而是用条款编号 + 归一化正文匹配。
- 全角/半角、空格、制表符差异统一用 NFKC + 去空白归一化后再比较。
- 定位失败（段落被整段重写）时记日志跳过，宁可不标也不标错段。
"""
from __future__ import annotations

import logging
import re
import unicodedata
from pathlib import Path

from docx import Document  # type: ignore[import-untyped]
from docx.document import Document as _Doc  # type: ignore[import-untyped]
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.table import Table  # type: ignore[import-untyped]
from docx.text.paragraph import Paragraph  # type: ignore[import-untyped]

from ..models import Diff, TamperReport

logger = logging.getLogger(__name__)

# 高亮配色（w:shd fill，RRGGBB）
# modified：黄（默认），按风险等级微调让严重程度一眼可辨
_SHADE_BY_RISK = {
    "high": "FFEB3B",   # 亮黄
    "medium": "FFE082", # 暖黄
    "low": "FFF59D",    # 浅黄
    "none": "EEEEEE",   # 灰
}
# deleted（PDF 缺失、Word 独有）：浅红
_SHADE_DELETED = "FFCDD2"
# added（PDF 独有、Word 无对应段落）：无法在源 docx 上标，跳过


def burn_docx(
    word_path: str | Path,
    report: TamperReport,
    output_path: str | Path,
) -> Path:
    """在源 docx 上烧录差异高亮，保存为新文件。

    - modified 条款：定位到 docx 对应段落，整段加黄底（按 risk_level 深浅）。
    - deleted 条款（Word 独有、PDF 缺失）：整段浅红底。
    - added 条款（PDF 独有）：源 docx 无对应段落，跳过（记日志）。

    返回输出文件路径。
    """
    word_path = Path(word_path)
    output_path = Path(output_path)
    doc = Document(str(word_path))

    # 收集所有段落（含表格内），保留对原 Paragraph 对象的引用以便改底纹
    paragraphs = list(_iter_all_paragraphs(doc))
    # 归一化文本 → 段落对象列表（同一段落可能归一化后与多条 diff 匹配）
    norm_index = [(_compact(p.text), p) for p in paragraphs]

    # 待标注的 diff（modified + deleted）。added 无法在源 docx 标。
    targets: list[Diff] = []
    targets.extend(d for d in report.diffs if d.status == "modified")
    targets.extend(
        d for d in report.unmatched_clauses
        if d.status == "deleted" and d.alignment_id  # 有内容的删除项
    )
    # added 统计用于日志
    n_added = sum(1 for d in report.unmatched_clauses if d.status == "added")

    marked = 0
    skipped_not_found = 0
    for diff in targets:
        para = _locate_paragraph(diff, norm_index)
        if para is None:
            skipped_not_found += 1
            logger.info(
                "docx burn skip (paragraph not located) number=%r status=%s",
                diff.number, diff.status,
            )
            continue
        fill = _SHADE_DELETED if diff.status == "deleted" else _SHADE_BY_RISK.get(
            diff.risk_level, _SHADE_BY_RISK["medium"]
        )
        _apply_shade(para, fill)
        marked += 1

    output_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(output_path))
    logger.info(
        "docx burn done targets=%s marked=%s not_found=%s added_skipped=%s out=%s",
        len(targets), marked, skipped_not_found, n_added, output_path.name,
    )
    return output_path


def build_docx_preview(
    word_path: str | Path,
    report: TamperReport,
) -> list[dict]:
    """解析源 docx，返回带差异标记的全段落列表（供前端 HTML 渲染在线预览）。

    与 burn_docx 同源的段落定位逻辑：把报告里的 modified/deleted 条款
    映射回 docx 段落，命中的段落带 highlight 标记，前端整段加底色。

    返回 [{text, highlight, status, risk_level, number}, ...]，按文档流顺序。
    - highlight: "" 未标记 / "modified" 黄 / "deleted" 红
    - 其余字段来自命中的 diff（未命中段落 status="identical"）
    """
    word_path = Path(word_path)
    doc = Document(str(word_path))
    paragraphs = list(_iter_all_paragraphs(doc))
    norm_index = [(_compact(p.text), p) for p in paragraphs]

    # 待标注的 diff（与 burn_docx 一致：modified + deleted）
    targets: list[Diff] = []
    targets.extend(d for d in report.diffs if d.status == "modified")
    targets.extend(
        d for d in report.unmatched_clauses
        if d.status == "deleted" and d.alignment_id
    )

    # 段落 id() → 命中的 diff，用于渲染时上标记
    para_marks: dict[int, Diff] = {}
    not_found = 0
    for diff in targets:
        para = _locate_paragraph(diff, norm_index)
        if para is None:
            not_found += 1
            continue
        para_marks[id(para)] = diff

    result: list[dict] = []
    for p in paragraphs:
        text = p.text.strip()
        if not text:
            continue
        diff = para_marks.get(id(p))
        if diff is None:
            result.append({
                "text": text, "highlight": "", "status": "identical",
                "risk_level": "none", "number": "",
            })
        else:
            result.append({
                "text": text,
                "highlight": diff.status,  # "modified" | "deleted"
                "status": diff.status,
                "risk_level": diff.risk_level,
                "number": diff.number,
            })
    logger.info(
        "docx preview built paragraphs=%s marked=%s not_found=%s",
        len(result), len(para_marks), not_found,
    )
    return result


# —— 段落定位 ——


def _locate_paragraph(
    diff: Diff, norm_index: list[tuple[str, Paragraph]]
) -> Paragraph | None:
    """把一条 diff 定位回 docx 段落。

    策略（兼容不同 docx 切分/编号写法）：
    1. 有 number：找以「number + 分隔符」开头的段落（容全角空格/制表符/多空格）。
       唯一即命中；多候选用正文相似度消歧。
    2. 无 number：用 diff 的 word 正文（从 segments 重建 equal+delete）做最长公共子串匹配。
    """
    # diff 的 word 侧归一化正文
    word_text = _compact(_word_text_from_segments(diff))

    if diff.number:
        candidates = [
            (norm_text, p) for norm_text, p in norm_index
            if _starts_with_number(norm_text, diff.number)
        ]
        if len(candidates) == 1:
            return candidates[0][1]
        if len(candidates) > 1:
            # 多候选：取与 word_text 最相似的（最长公共子串比例）
            return max(candidates, key=lambda item: _lcs_ratio(word_text, item[0]))[1]

    # 无编号或编号未命中：退化为正文最长公共子串匹配
    if not word_text:
        return None
    best: Paragraph | None = None
    best_score = 0.0
    for norm_text, p in norm_index:
        if not norm_text:
            continue
        score = _lcs_ratio(word_text, norm_text)
        if score > best_score:
            best_score = score
            best = p
    # 相似度太低视为未命中（避免把完全不相关的段落误标）
    if best_score < 0.5:
        return None
    return best


def _starts_with_number(norm_text: str, number: str) -> bool:
    """归一化后的段落文本是否以「编号 + 分隔符」开头。

    分隔符容全角/半角空格、制表符、顿号、点等（已被 NFKC 折叠，这里主要匹配
    编号 token 紧跟非数字非字母的边界，避免 3.2 误命中 3.21）。
    """
    number_n = _compact(number)
    if not number_n:
        return False
    if not norm_text.startswith(number_n):
        return False
    rest = norm_text[len(number_n):]
    # 紧跟分隔符或直接是中文/标点均可；避免「3.2」前缀误命中「3.21」
    if not rest:
        return True
    # rest 首字符是数字或字母（说明编号被更长数字吞掉）→ 不算
    return not (rest[0].isalnum() and rest[0].isascii())


def _word_text_from_segments(diff: Diff) -> str:
    """从 char_diff segments 重建 word 侧文本（equal + delete）。"""
    return "".join(s.text for s in diff.segments if s.op in ("equal", "delete"))


# —— 文本归一化（用于段落匹配，容忍全角/半角/空白差异）——


def _compact(text: str) -> str:
    """NFKC + 去除所有空白，用于段落匹配比较键。"""
    if not text:
        return ""
    s = unicodedata.normalize("NFKC", text)
    return re.sub(r"\s+", "", s)


def _lcs_ratio(a: str, b: str) -> float:
    """最长公共子串长度 / 较短串长度，粗粒度相似度（0~1）。

    用子串而非编辑距离，O(n*m) 对单条款长度（通常 < 300 字符）足够。
    """
    if not a or not b:
        return 0.0
    la, lb = len(a), len(b)
    # 滚动数组求最长公共子串
    prev = [0] * (lb + 1)
    best = 0
    for i in range(1, la + 1):
        cur = [0] * (lb + 1)
        ai = a[i - 1]
        for j in range(1, lb + 1):
            if ai == b[j - 1]:
                cur[j] = prev[j - 1] + 1
                if cur[j] > best:
                    best = cur[j]
        prev = cur
    return best / min(la, lb)


# —— 底纹应用（不动 run，保留原格式）——


def _apply_shade(paragraph: Paragraph, fill: str) -> None:
    """给段落加整段底纹（w:shd），不修改任何 run。"""
    pPr = paragraph._p.get_or_add_pPr()
    # 若已有 shd 先移除，避免重复标注时叠加
    existing = pPr.find(qn("w:shd"))
    if existing is not None:
        pPr.remove(existing)
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), fill)
    pPr.append(shd)


def _iter_all_paragraphs(doc: _Doc):
    """遍历文档所有段落（顶层 + 表格单元格内），保持文档流顺序。"""
    for p in doc.paragraphs:
        yield p
    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                for p in cell.paragraphs:
                    yield p
