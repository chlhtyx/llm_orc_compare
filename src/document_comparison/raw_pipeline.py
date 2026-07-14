"""无标注版比对流水线:Word→纯文本、PDF→纯文本、difflib 行级比对。

与 pipeline.py(TamperReport 体系)完全独立,不经过条款对齐/风险分级。
OCR 复用现有 LLMOCREngine,只多一步 flatten blocks→纯文本。
"""
from __future__ import annotations

import logging
from difflib import SequenceMatcher
from pathlib import Path
from typing import Callable

from .compare.diff import char_diff
from .config import Settings, settings
from .models import TextDiffHunk, TextDiffReport
from .ocr import get_ocr_engine
from .parsing import get_page_metas, parse_word
from .structure.normalize import normalize_text

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
    word_raw = parse_word(word_path)
    word_text = "\n".join(item.text for item in word_raw if item.text)
    logger.info("word parsed items=%s chars=%s", len(word_raw), len(word_text))
    _progress("word_done", 0.10)

    # —— ② PDF → 纯文本(OCR 复用现有引擎,flatten blocks)——
    _progress("ocr", 0.12)
    page_metas = get_page_metas(pdf_path, cfg.pdf_render_dpi)
    pages_blocks = ocr.recognize(Path(pdf_path), page_metas, on_progress=_progress)
    logger.info(
        "ocr done pages=%s blocks=%s dpi=%s",
        len(page_metas), sum(len(b) for b in pages_blocks), cfg.pdf_render_dpi,
    )
    _progress("ocr_done", 0.90)
    # 每页块的 content 用换行拼接,页间再换行
    page_texts = ["\n".join(b.content for b in blocks if b.content) for blocks in pages_blocks]
    pdf_text = "\n".join(t for t in page_texts if t)

    # —— ③ 标准化(NFKC、空白归一)——
    _progress("normalize", 0.92)
    word_text = normalize_text(word_text)
    pdf_text = normalize_text(pdf_text)

    # —— ④ difflib 行级比对 → hunks ——
    _progress("diff", 0.95)
    report = _diff_texts(word_text, pdf_text, char_level=char_level,
                         source=str(word_path), target=str(pdf_path))
    logger.info(
        "diff done hunks=%s similarity=%s word_lines=%s pdf_lines=%s",
        len(report.hunks), report.stats.get("similarity"),
        report.stats.get("word_total_lines"), report.stats.get("pdf_total_lines"),
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
    """对两段纯文本做行级 SequenceMatcher,产出差异片段报告。"""
    word_lines = word_text.splitlines()
    pdf_lines = pdf_text.splitlines()
    sm = SequenceMatcher(a=word_lines, b=pdf_lines, autojunk=False)
    ratio = sm.ratio()

    hunks: list[TextDiffHunk] = []
    equal_lines = replaced = deleted = inserted = 0
    opcodes = sm.get_opcodes()

    for idx, (tag, i1, i2, j1, j2) in enumerate(opcodes):
        if tag == "equal":
            equal_lines += (i2 - i1)
            continue
        # 差异行
        wl = word_lines[i1:i2]
        pl = pdf_lines[j1:j2]
        if tag == "replace":
            replaced += max(len(wl), len(pl))
        elif tag == "delete":
            deleted += len(wl)
        elif tag == "insert":
            inserted += len(pl)

        # 上下文:前一个 equal 的末尾 / 后一个 equal 的开头
        ctx_before = _take_context(word_lines, i1, before=True)
        ctx_after = _take_context(word_lines, i2 - 1, before=False)

        # 字符级细化:仅 replace 且单行对单行时跑 char_diff(多行 replace 字符级意义不大)
        char_segs = []
        if char_level and tag == "replace" and len(wl) == 1 and len(pl) == 1:
            char_segs = char_diff(wl[0], pl[0])

        hunks.append(TextDiffHunk(
            tag=tag,  # type: ignore[arg-type]
            word_lines=wl,
            pdf_lines=pl,
            char_segments=char_segs,
            context_before=ctx_before,
            context_after=ctx_after,
        ))

    stats = {
        "equal_lines": equal_lines,
        "replaced": replaced,
        "deleted": deleted,
        "inserted": inserted,
        "similarity": round(ratio, 4),
        "word_total_lines": len(word_lines),
        "pdf_total_lines": len(pdf_lines),
    }
    return TextDiffReport(
        source=source,
        target=target,
        word_text=word_text,
        pdf_text=pdf_text,
        hunks=hunks,
        stats=stats,
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
