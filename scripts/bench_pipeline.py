"""真实样本耗时实测脚本:对 run_pipeline 做阶段级计时。

复用 settings 的真实配置(云端 LLM OCR),在每个阶段前后采样 perf_counter,
输出 per-stage 与总耗时。可指定样本或扫 uploads 目录全部样本。

用法:
  .venv/bin/python -m scripts.bench_pipeline                  # 跑 uploads 第一对
  .venv/bin/python -m scripts.bench_pipeline --all            # 跑全部样本
  .venv/bin/python -m scripts.bench_pipeline --source X --target Y
"""
from __future__ import annotations

import argparse
import glob
import os
import sys
import time
from pathlib import Path

# 让 src/ 下的包可直接 import(无需安装)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from document_comparison.config import settings  # noqa: E402
from document_comparison.embed import get_embed_engine, is_mock_engine  # noqa: E402
from document_comparison.ocr import get_ocr_engine  # noqa: E402
from document_comparison.parsing import get_page_metas, parse_word  # noqa: E402
from document_comparison.structure import blocks_to_raw, build_clauses  # noqa: E402
from document_comparison.align import align_clauses  # noqa: E402
from document_comparison.report import build_report  # noqa: E402


def _pair_up(uploads_dir: Path) -> list[tuple[Path, Path]]:
    sources = sorted(glob.glob(str(uploads_dir / "*-source.docx")))
    pairs = []
    for s in sources:
        t = s.replace("-source.docx", "-target.pdf")
        if os.path.exists(t):
            pairs.append((Path(s), Path(t)))
    return pairs


def bench_one(word_path: Path, pdf_path: Path) -> dict:
    """跑一遍完整链路并逐段计时。返回各阶段耗时(秒)与产出规模。"""
    timings: dict[str, float] = {}
    cfg = settings
    ocr = get_ocr_engine()
    embed = get_embed_engine(cfg.embed_backend)
    thresholds = {"identical": cfg.similarity_identical, "modified": cfg.similarity_modified}
    align_threshold = cfg.align_similarity_mock if is_mock_engine(embed) else cfg.align_similarity

    sizes: dict[str, int] = {}

    # —— ① Word 解析 + 切分 ——
    t0 = time.perf_counter()
    word_raw = parse_word(word_path)
    word_clauses = build_clauses(word_raw, "word")
    timings["word_parse+split"] = time.perf_counter() - t0
    sizes["word_blocks"] = len(word_raw)
    sizes["word_clauses"] = len(word_clauses)

    # —— ① PDF 取 meta + 渲染 ——
    t0 = time.perf_counter()
    page_metas = get_page_metas(pdf_path, cfg.pdf_render_dpi)
    timings["pdf_get_metas"] = time.perf_counter() - t0
    sizes["pdf_pages"] = len(page_metas)

    # —— ② OCR(含渲染,在 ocr.recognize 内部)——
    t0 = time.perf_counter()
    pages_blocks = ocr.recognize(Path(pdf_path), page_metas)
    timings["ocr(recognize)"] = time.perf_counter() - t0

    # —— ③ PDF 切分 ——
    t0 = time.perf_counter()
    pdf_raw = blocks_to_raw(pages_blocks)
    pdf_clauses = build_clauses(pdf_raw, "pdf")
    timings["pdf_split"] = time.perf_counter() - t0
    sizes["pdf_clauses"] = len(pdf_clauses)

    # —— ④ 对齐 ——
    t0 = time.perf_counter()
    alignments = align_clauses(word_clauses, pdf_clauses, embed, align_threshold)
    timings["align"] = time.perf_counter() - t0
    sizes["alignments"] = len(alignments)

    # —— ⑤⑥ 比对 + 报告 ——
    t0 = time.perf_counter()
    word_by = {c.clause_id: c for c in word_clauses}
    pdf_by = {c.clause_id: c for c in pdf_clauses}
    report = build_report(
        alignments=alignments, word_by=word_by, pdf_by=pdf_by,
        embed=embed, page_metas=page_metas, thresholds=thresholds,
        source=str(word_path), target=str(pdf_path),
    )
    timings["compare+report"] = time.perf_counter() - t0
    sizes["diffs"] = len(report.diffs)
    sizes["unmatched"] = len(report.unmatched_clauses)
    sizes["overall_risk"] = report.overall_risk

    timings["__total__"] = sum(v for k, v in timings.items() if not k.startswith("__"))
    return {"timings": timings, "sizes": sizes}


def _fmt_report(name: str, r: dict) -> str:
    t = r["timings"]
    total = t["__total__"]
    lines = [f"\n=== {name} ==="]
    lines.append(f"总耗时: {total:.3f}s")
    lines.append("阶段明细:")
    for stage, sec in t.items():
        if stage.startswith("__"):
            continue
        pct = (sec / total * 100) if total > 0 else 0
        lines.append(f"  {stage:<20} {sec:>8.3f}s  ({pct:5.1f}%)")
    lines.append("产出规模:")
    for k, v in r["sizes"].items():
        lines.append(f"  {k:<20} {v}")
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", help="Word 路径")
    ap.add_argument("--target", help="PDF 路径")
    ap.add_argument("--all", action="store_true", help="扫 uploads 全部样本")
    args = ap.parse_args()

    print(f"配置: ocr={settings.llm_model} @ {settings.llm_api_base}")
    print(f"      embed_backend={settings.embed_backend}"
          f" (mock={is_mock_engine(get_embed_engine(settings.embed_backend))})")
    print(f"      dpi={settings.pdf_render_dpi} concurrency={settings.llm_max_concurrency}")

    if args.source and args.target:
        pairs = [(Path(args.source), Path(args.target))]
    else:
        pairs = _pair_up(settings.storage_dir / "uploads")
        if not args.all:
            pairs = pairs[:1]

    if not pairs:
        print("未找到样本(.dc_data/uploads 下需有 *-source.docx + *-target.pdf)")
        return

    results = []
    for i, (w, p) in enumerate(pairs, 1):
        name = f"样本{i}: {w.parent.name}/{w.name.split('-')[0]}"
        try:
            r = bench_one(w, p)
            print(_fmt_report(name, r))
            results.append((name, r))
        except Exception as e:  # noqa: BLE001
            print(f"\n=== {name} ===\n失败: {type(e).__name__}: {e}")

    # 多样本汇总
    if len(results) > 1:
        print("\n" + "=" * 60)
        print("汇总(均值):")
        stages = [k for k in results[0][1]["timings"] if not k.startswith("__")]
        for st in stages:
            vals = [r[1]["timings"][st] for r in results]
            print(f"  {st:<20} mean={sum(vals)/len(vals):.3f}s  "
                  f"min={min(vals):.3f}s  max={max(vals):.3f}s")
        totals = [r[1]["timings"]["__total__"] for r in results]
        print(f"  {'__total__':<20} mean={sum(totals)/len(totals):.3f}s  "
              f"min={min(totals):.3f}s  max={max(totals):.3f}s")


if __name__ == "__main__":
    main()
