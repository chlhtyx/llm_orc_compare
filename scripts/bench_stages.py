"""本地阶段规模测试:不调 OCR/embed API,纯测 align + compare+report 随条款数的耗时。

用合成条款验证:非 OCR 阶段在大文档(几十~几百条款)下是否仍可忽略。
embed 用 mock(字符袋相似度,纯本地)。
"""
from __future__ import annotations

import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from document_comparison.config import settings  # noqa: E402
from document_comparison.embed import get_embed_engine, is_mock_engine  # noqa: E402
from document_comparison.align import align_clauses  # noqa: E402
from document_comparison.report import build_report  # noqa: E402
from document_comparison.models import Block, Clause, PageMeta  # noqa: E402


def _make_clause(i: int, src: str) -> Clause:
    """造一条带编号、带正文、带一个 block 的条款。"""
    text = f"第{i}条 甲方应于本合同签署之日起 {i} 个工作日内,向乙方支付首期款项人民币{random.randint(1,99)}万元整。"
    return Clause(
        clause_id=f"{src}-c{i}",
        doc_type=src,  # "word" | "pdf"
        number=str(i),
        level=1,
        title=f"第{i}条 付款义务",
        text=text,
        blocks=[Block(block_id=f"{src}-b{i}", page_index=0,
                      label="text", bbox=[0, 0, 100, 20], content=text)],
    )


def bench_at_size(n: int) -> dict:
    embed = get_embed_engine(settings.embed_backend)
    threshold = settings.align_similarity_mock if is_mock_engine(embed) else settings.align_similarity
    thresholds = {"identical": settings.similarity_identical,
                  "modified": settings.similarity_modified}

    # word 全部条款;pdf 篡改约 20% 条款的内容(制造 diff)
    word_clauses = [_make_clause(i, "word") for i in range(1, n + 1)]
    pdf_clauses = []
    for i in range(1, n + 1):
        c = _make_clause(i, "pdf")
        if i % 5 == 0:  # 每 5 条改 1 条
            c.text = c.text.replace("支付首期款项", "一次性支付全款")
        pdf_clauses.append(c)

    page_metas = [PageMeta(page_index=0, width_px=2480, height_px=3508,
                           pdf_width_pt=595.0, pdf_height_pt=842.0)]

    t0 = time.perf_counter()
    alignments = align_clauses(word_clauses, pdf_clauses, embed, threshold)
    t_align = time.perf_counter() - t0

    t0 = time.perf_counter()
    word_by = {c.clause_id: c for c in word_clauses}
    pdf_by = {c.clause_id: c for c in pdf_clauses}
    report = build_report(
        alignments=alignments, word_by=word_by, pdf_by=pdf_by,
        embed=embed, page_metas=page_metas, thresholds=thresholds,
        source="w", target="p",
    )
    t_report = time.perf_counter() - t0

    return {
        "n_clauses": n,
        "align_s": round(t_align, 4),
        "compare_report_s": round(t_report, 4),
        "diffs": len(report.diffs),
        "unmatched": len(report.unmatched_clauses),
    }


def main() -> None:
    print("本地阶段规模测试(mock embed,纯本地,无网络)\n")
    print(f"{'条款数':>6}  {'对齐(s)':>10}  {'比对+报告(s)':>14}  {'diffs':>6}  {'unmatched':>10}")
    for n in (10, 30, 50, 100, 200, 400):
        r = bench_at_size(n)
        print(f"{r['n_clauses']:>6}  {r['align_s']:>10.4f}  "
              f"{r['compare_report_s']:>14.4f}  {r['diffs']:>6}  {r['unmatched']:>10}")


if __name__ == "__main__":
    main()
