"""条款对齐:三层兜底策略(§5.4)。

1. 编号锚定:按 number 精确匹配(篡改通常改内容不改编号)。
2. 语义向量匹配:对未配对条款用相似度最近邻,阈值确认。
3. 仍未配对 → added(pdf 独有)/ deleted(word 缺失)。

> 顺序敏感:合同条款顺序稳定,可进一步约束为「编号邻域优先搜索」,
> 本实现先用全局最近邻(结果正确,可后续优化为邻域窗口以降低远距离误配)。
"""
from __future__ import annotations

from ..models import Alignment, Clause


def align_clauses(
    word_clauses: list[Clause],
    pdf_clauses: list[Clause],
    embed,
    threshold: float,
) -> list[Alignment]:
    w_by_num = {c.number: c for c in word_clauses if c.number}
    p_by_num = {c.number: c for c in pdf_clauses if c.number}

    matched_w: set[str] = set()
    matched_p: set[str] = set()
    alignments: list[Alignment] = []

    # 1. 编号锚定
    for number, pc in p_by_num.items():
        wc = w_by_num.get(number)
        if wc:
            alignments.append(
                Alignment(
                    word_clause_id=wc.clause_id,
                    pdf_clause_id=pc.clause_id,
                    match_type="number",
                    similarity=1.0,
                )
            )
            matched_w.add(wc.clause_id)
            matched_p.add(pc.clause_id)

    # 2. 语义向量匹配
    rem_w = [c for c in word_clauses if c.clause_id not in matched_w]
    rem_p = [c for c in pdf_clauses if c.clause_id not in matched_p]
    w_vecs = [(c, embed.embed(c.text)) for c in rem_w]

    for pc in rem_p:
        pv = embed.embed(pc.text)
        best: Clause | None = None
        best_sim = -1.0
        for wc, wv in w_vecs:
            s = embed.similarity(pv, wv)
            if s > best_sim:
                best, best_sim = wc, s
        if best is not None and best_sim >= threshold:
            alignments.append(
                Alignment(
                    word_clause_id=best.clause_id,
                    pdf_clause_id=pc.clause_id,
                    match_type="semantic",
                    similarity=best_sim,
                )
            )
            matched_w.add(best.clause_id)
            w_vecs = [(c, v) for c, v in w_vecs if c.clause_id != best.clause_id]
        else:
            # pdf 独有 → added
            alignments.append(
                Alignment(
                    word_clause_id=None,
                    pdf_clause_id=pc.clause_id,
                    match_type="unmatched",
                    similarity=max(best_sim, 0.0),
                )
            )

    # 3. word 剩余 → deleted
    for wc in rem_w:
        if wc.clause_id not in matched_w:
            alignments.append(
                Alignment(
                    word_clause_id=wc.clause_id,
                    pdf_clause_id=None,
                    match_type="unmatched",
                    similarity=0.0,
                )
            )

    return alignments
