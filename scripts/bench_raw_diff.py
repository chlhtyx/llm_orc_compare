"""无标注版 diff 阶段性能对比:全量 SequenceMatcher vs 分块锚定 diff。

用 data/uploads 下的真实 docx/pdf 对(走 PDF 文本层 mock OCR,避开 LLM),
测量 _diff_texts 的纯 diff 耗时,并校验新旧实现统计口径一致。

用法:
    python -m scripts.bench_raw_diff
    python -m scripts.bench_raw_diff --pair d097b5ba79104c02
"""
from __future__ import annotations

import argparse
import io
import time
from difflib import SequenceMatcher
from pathlib import Path

from document_comparison.models import PageMeta, TextDiffHunk
from document_comparison.parsing.pdf import extract_text_blocks
from document_comparison.parsing.word import parse_word
from document_comparison.raw_pipeline import _diff_texts, _find_common_anchors
from document_comparison.structure.normalize import normalize_text

try:
    import pymupdf as fitz
except ImportError:
    import fitz  # type: ignore

UPLOADS = Path(__file__).resolve().parent.parent / "data" / "uploads"


def _text_layer_ocr(pdf_path: Path):
    blocks_per_page = extract_text_blocks(pdf_path)
    return blocks_per_page


def _old_diff(word_text: str, pdf_text: str, *, char_level: bool):
    """原全量实现(仅用于基准对比,不进生产代码)。"""
    word_lines = word_text.splitlines()
    pdf_lines = pdf_text.splitlines()
    sm = SequenceMatcher(a=word_lines, b=pdf_lines, autojunk=False)
    ratio = sm.ratio()
    stats = {"equal_lines": 0, "replaced": 0, "deleted": 0, "inserted": 0}
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            stats["equal_lines"] += i2 - i1
            continue
        wl = word_lines[i1:i2]
        pl = pdf_lines[j1:j2]
        if tag == "replace":
            stats["replaced"] += max(len(wl), len(pl))
        elif tag == "delete":
            stats["deleted"] += len(wl)
        elif tag == "insert":
            stats["inserted"] += len(pl)
    tot = len(word_lines) + len(pdf_lines)
    stats["similarity"] = round((2.0 * stats["equal_lines"] / tot) if tot else 1.0, 4)
    stats["word_total_lines"] = len(word_lines)
    stats["pdf_total_lines"] = len(pdf_lines)
    return stats


def _load_pair(key: str) -> tuple[str, str]:
    wpath = UPLOADS / f"{key}-source.docx"
    ppath = UPLOADS / f"{key}-target.pdf"
    if not wpath.exists() or not ppath.exists():
        raise FileNotFoundError(f"pair not found for key={key}: {wpath} / {ppath}")
    word_raw = parse_word(wpath)
    word_text = "\n".join(item.text for item in word_raw if item.text)
    blocks = _text_layer_ocr(ppath)
    page_texts = ["\n".join(b.content for b in pg if b.content) for pg in blocks]
    pdf_text = "\n".join(t for t in page_texts if t)
    return normalize_text(word_text), normalize_text(pdf_text)


def _bench(fn, *args, repeat: int = 3, **kwargs):
    best = float("inf")
    for _ in range(repeat):
        t0 = time.perf_counter()
        out = fn(*args, **kwargs)
        best = min(best, time.perf_counter() - t0)
    return best, out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pair", default=None, help="uploads key, e.g. d097b5ba79104c02")
    args = parser.parse_args()

    keys: list[str]
    if args.pair:
        keys = [args.pair]
    else:
        keys = sorted({
            p.name.split("-source.docx")[0]
            for p in UPLOADS.glob("*-source.docx")
            if p.exists()
        })

    if not keys:
        print("no pairs found under", UPLOADS)
        return

    for key in keys:
        try:
            word_text, pdf_text = _load_pair(key)
        except Exception as e:  # noqa: BLE001
            print(f"[{key}] skip: {e}")
            continue

        wl = word_text.splitlines()
        pl = pdf_text.splitlines()
        anchors = _find_common_anchors(wl, pl)
        print(f"\n=== pair {key} ===")
        print(f"  word_lines={len(wl)} pdf_lines={len(pl)} anchors={len(anchors)}")

        t_old, old_stats = _bench(_old_diff, word_text, pdf_text, char_level=True)
        t_new, new_report = _bench(_diff_texts, word_text, pdf_text,
                                   char_level=True, source="", target="")
        new_stats = new_report.stats

        # 一致性校验:核心统计字段应完全一致
        mismatch = {
            k: (old_stats.get(k), new_stats.get(k))
            for k in ("equal_lines", "replaced", "deleted", "inserted", "similarity")
            if old_stats.get(k) != new_stats.get(k)
        }

        print(f"  old(全量): {t_old*1000:8.1f} ms")
        print(f"  new(分块): {t_new*1000:8.1f} ms")
        if t_new > 0:
            print(f"  加速比:    {t_old/t_new:8.2f}x")
        print(f"  hunks(new)={len(new_report.hunks)} similarity={new_stats['similarity']}")
        if mismatch:
            print(f"  ⚠ 统计不一致: {mismatch}")
        else:
            print("  ✓ 统计一致(equal/replaced/deleted/inserted/similarity)")


if __name__ == "__main__":
    main()
