"""无标注版比对流水线:Word→纯文本、PDF→纯文本、difflib 行级比对。

与 pipeline.py(TamperReport 体系)完全独立,不经过条款对齐/风险分级。
OCR 复用现有 LLMOCREngine,只多一步 flatten blocks→纯文本。
"""
from __future__ import annotations

import logging
import time
from collections import Counter
from difflib import SequenceMatcher
from pathlib import Path
from typing import Callable

from .compare.diff import char_diff
from .config import Settings, settings
from .models import TextDiffHunk, TextDiffReport
from .ocr import get_ocr_engine
from .ocr.quality import attach_raw_recognition_diagnostics
from .observability import timed_stage
from .parsing import get_page_metas, parse_word
from .structure.normalize import normalize_table_text, normalize_text

logger = logging.getLogger(__name__)

ProgressCb = Callable[[str, float], None]

# 上下文行数:每个差异片段前后各保留 N 行 equal 作上下文。
_CONTEXT_LINES = 1


def run_raw_pipeline(
    word_path: str | Path,
    pdf_path: str | Path,
    *,
    cfg: Settings | None = None,
    ocr=None,
    on_progress: ProgressCb | None = None,
    char_level: bool = True,
) -> TextDiffReport:
    cfg = cfg or settings
    ocr = ocr or get_ocr_engine()

    def _progress(stage: str, frac: float) -> None:
        if on_progress:
            on_progress(stage, frac)

    # —— ① Word → 纯文本(不走 build_clauses)——
    _progress("word_parsing", 0.02)
    with timed_stage(logger, "raw_word_parse"):
        word_raw = parse_word(word_path)
        word_text = "\n".join(item.text for item in word_raw if item.text)
    logger.info("word flattened items=%s chars=%s", len(word_raw), len(word_text))
    _progress("word_done", 0.10)

    # —— ② PDF → 纯文本(OCR 复用现有引擎,flatten blocks)——
    _progress("ocr", 0.12)
    with timed_stage(logger, "raw_pdf_metadata"):
        page_metas = get_page_metas(pdf_path, cfg.pdf_render_dpi)
    with timed_stage(logger, "raw_pdf_ocr", pages=len(page_metas)):
        pages_blocks = ocr.recognize(Path(pdf_path), page_metas, on_progress=_progress)
    logger.info(
        "ocr done pages=%s blocks=%s dpi=%s",
        len(page_metas), sum(len(b) for b in pages_blocks), cfg.pdf_render_dpi,
    )
    _progress("ocr_done", 0.90)
    # 每页块的 content 用换行拼接,页间再换行
    # 表格 block 单独走表格规范化,与 Word 侧 _table_to_text 统一为
    # 「cell | cell」格式,消除两端表达同一张表时的文本差异
    # (OCR 可能返回 Markdown / TSV / 首尾包裹管线等多种格式)。
    def _block_text(b):
        if b.label == "table":
            return normalize_table_text(b.content)
        return b.content

    page_texts = ["\n".join(_block_text(b) for b in blocks if b.content) for blocks in pages_blocks]
    pdf_text = "\n".join(t for t in page_texts if t)

    # —— ③ 标准化(NFKC、空白归一)——
    _progress("normalize", 0.92)
    with timed_stage(logger, "raw_normalize"):
        word_text = normalize_text(word_text)
        pdf_text = normalize_text(pdf_text)

    # —— ④ difflib 行级比对 → hunks ——
    _progress("diff", 0.95)
    t0 = time.perf_counter()
    with timed_stage(logger, "raw_diff"):
        report = _diff_texts(word_text, pdf_text, char_level=char_level,
                             source=str(word_path), target=str(pdf_path))
        diagnostics = list(getattr(ocr, "last_diagnostics", []))
        attach_raw_recognition_diagnostics(report, diagnostics)
    logger.info(
        "diff stage cost=%.3fs word_lines=%s pdf_lines=%s hunks=%s similarity=%s",
        time.perf_counter() - t0,
        report.stats.get("word_total_lines"), report.stats.get("pdf_total_lines"),
        len(report.hunks), report.stats.get("similarity"),
    )
    _progress("done", 1.0)
    return report


def _diff_texts(
    word_text: str,
    pdf_text: str,
    *,
    char_level: bool,
    source: str,
    target: str,
) -> TextDiffReport:
    """对两段纯文本做行级比对,产出差异片段报告。

    优化:先用「两边都唯一的相同行」作为锚点把全文切成多个小区间,
    每个区间内单独跑 SequenceMatcher。锚点本身两侧完全相同,直接计入 equal;
    区间内才是真正的差异区域,规模远小于全文,避免一次全量 O(n·m) 的纯 Python diff。
    退化为单次全量比对的兜底也保留:无锚点时等价于原实现。
    """
    word_lines = word_text.splitlines()
    pdf_lines = pdf_text.splitlines()

    hunks: list[TextDiffHunk] = []
    stats = {"equal_lines": 0, "replaced": 0, "deleted": 0, "inserted": 0}

    anchors = _find_common_anchors(word_lines, pdf_lines)

    prev_w = 0
    prev_p = 0
    for w_idx, p_idx in anchors:
        # 锚点之前的区间(可能含差异)
        _diff_block(
            word_lines, pdf_lines,
            w_lo=prev_w, w_hi=w_idx, p_lo=prev_p, p_hi=p_idx,
            char_level=char_level, word_full=word_lines,
            out_hunks=hunks, stats=stats,
        )
        # 锚点行本身两侧相同,计入 equal
        stats["equal_lines"] += 1
        prev_w = w_idx + 1
        prev_p = p_idx + 1
    # 末尾区间
    _diff_block(
        word_lines, pdf_lines,
        w_lo=prev_w, w_hi=len(word_lines), p_lo=prev_p, p_hi=len(pdf_lines),
        char_level=char_level, word_full=word_lines,
        out_hunks=hunks, stats=stats,
    )

    # 相似度:基于已统计的行数,与 difflib.ratio() 同口径(2M / (T + T'))
    eq = stats["equal_lines"]
    tot = len(word_lines) + len(pdf_lines)
    ratio = (2.0 * eq / tot) if tot else 1.0

    stats["similarity"] = round(ratio, 4)
    stats["word_total_lines"] = len(word_lines)
    stats["pdf_total_lines"] = len(pdf_lines)

    return TextDiffReport(
        source=source,
        target=target,
        word_text=word_text,
        pdf_text=pdf_text,
        hunks=hunks,
        stats=stats,
    )


def _find_common_anchors(
    word_lines: list[str], pdf_lines: list[str], *, min_gap: int = 1
) -> list[tuple[int, int]]:
    """找出两边「共同唯一行」作为锚点,用于把全文切成小区间分块 diff。

    只取在 word 侧唯一、且在 pdf 侧唯一、且文本相同的行作为锚点。
    这类行在两端都是确定性配对,可作为可靠的分块边界。
    返回按 word 序排列、且 pdf 索引严格递增的 (word_idx, pdf_idx) 锚点列表。

    Args:
        min_gap: 相邻两个锚点之间 word 侧行数不足 min_gap 的,
            跳过该锚点(并入上一个块),避免产生过多微小块。
    """
    if not word_lines or not pdf_lines:
        return []

    w_count = Counter(word_lines)
    p_count = Counter(pdf_lines)

    # 行文本 → pdf 中第一次出现的索引(仅当该行在 pdf 唯一时记录)
    p_index: dict[str, int] = {}
    for idx, line in enumerate(pdf_lines):
        if p_count[line] == 1 and line not in p_index:
            p_index[line] = idx

    anchors: list[tuple[int, int]] = []
    last_w = -1
    for w_idx, line in enumerate(word_lines):
        # 必须在两侧都唯一
        if w_count[line] != 1 or p_count.get(line, 0) != 1:
            continue
        p_idx = p_index[line]
        # 锚点在 word 侧递增时,pdf 索引也应严格递增,否则该行无法作边界
        if anchors and p_idx <= anchors[-1][1]:
            continue
        # 与上一个锚点距离太近则跳过,合并到上一块
        if w_idx - last_w < min_gap:
            continue
        anchors.append((w_idx, p_idx))
        last_w = w_idx
    return anchors


def _diff_block(
    word_lines: list[str],
    pdf_lines: list[str],
    *,
    w_lo: int, w_hi: int, p_lo: int, p_hi: int,
    char_level: bool,
    word_full: list[str],
    out_hunks: list[TextDiffHunk],
    stats: dict,
) -> None:
    """对 [w_lo, w_hi) × [p_lo, p_hi) 子区间跑 SequenceMatcher,差异追加到 out_hunks。

    边界语义:区间左闭右开。若区间内某侧为空(例如相邻锚点间 pdf 没有行),
    则整体视为 insert/delete 一次性计入,不再调 difflib。
    """
    wl = word_lines[w_lo:w_hi]
    pl = pdf_lines[p_lo:p_hi]

    if not wl and not pl:
        return
    if not wl:
        stats["inserted"] += len(pl)
        out_hunks.append(_make_hunk("insert", wl, pl, word_full, w_lo, w_hi, char_level))
        return
    if not pl:
        stats["deleted"] += len(wl)
        out_hunks.append(_make_hunk("delete", wl, pl, word_full, w_lo, w_hi, char_level))
        return

    sm = SequenceMatcher(a=wl, b=pl, autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            stats["equal_lines"] += (i2 - i1)
            continue
        sub_wl = wl[i1:i2]
        sub_pl = pl[j1:j2]
        if tag == "replace":
            stats["replaced"] += max(len(sub_wl), len(sub_pl))
        elif tag == "delete":
            stats["deleted"] += len(sub_wl)
        elif tag == "insert":
            stats["inserted"] += len(sub_pl)
        out_hunks.append(_make_hunk(
            tag, sub_wl, sub_pl, word_full, w_lo + i1, w_lo + i2, char_level,
        ))


def _make_hunk(
    tag: str,
    wl: list[str],
    pl: list[str],
    word_full: list[str],
    w_lo: int, w_hi: int,
    char_level: bool,
) -> TextDiffHunk:
    """构造一个 TextDiffHunk,上下文取自 word 全文的真实相邻行。"""
    ctx_before = _take_context(word_full, w_lo, before=True)
    ctx_after = _take_context(word_full, max(w_lo, w_hi - 1), before=False)

    char_segs = []
    if char_level and tag == "replace" and len(wl) == 1 and len(pl) == 1:
        char_segs = char_diff(wl[0], pl[0])

    return TextDiffHunk(
        tag=tag,  # type: ignore[arg-type]
        word_lines=wl,
        pdf_lines=pl,
        char_segments=char_segs,
        context_before=ctx_before,
        context_after=ctx_after,
    )


def _take_context(lines: list[str], anchor: int, *, before: bool) -> list[str]:
    """取 anchor 处前/后的 _CONTEXT_LINES 行(equal 上下文)。

    before=True 取 anchor 之前(不含 anchor);before=False 取 anchor 之后。
    越界自动收敛。
    """
    if not lines:
        return []
    if before:
        start = max(0, anchor - _CONTEXT_LINES + 1)
        return lines[start:anchor]
    end = min(len(lines), anchor + 1 + _CONTEXT_LINES)
    return lines[anchor + 1:end]
