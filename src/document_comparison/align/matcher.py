"""条款对齐:四层兜底策略(§5.4)。

1. 编号锚定:按 number 精确匹配(篡改通常改内容不改编号)。
2. 字段名锚定:首部/签字页等无编号键值块(甲方/乙方/地址/电话/日期 等)
   按 field_key 配对,解决两端切分边界不一致导致的误判 added/deleted。
   同一 field_key 多条时按出现顺序配对(甲方首部信息 + 甲方签字页是两条)。
3. 语义向量匹配:对未配对条款用相似度最近邻 + 邻域窗口约束,阈值确认。
4. 仍未配对 → added(pdf 独有)/ deleted(word 缺失)。

> 顺序敏感:合同条款顺序稳定,语义匹配采用「邻域窗口优先(±N 条),
> 窗口内无匹配再回退全局」,降低远距离误配。
"""
from __future__ import annotations

import logging
from collections import Counter

from ..models import Alignment, Clause

logger = logging.getLogger(__name__)

# 语义匹配的邻域窗口(按阅读顺序 ±N 条内优先搜索)
_NEIGHBORHOOD = 5


def align_clauses(
    word_clauses: list[Clause],
    pdf_clauses: list[Clause],
    embed,
    threshold: float,
) -> list[Alignment]:
    matched_w: set[str] = set()
    matched_p: set[str] = set()
    alignments: list[Alignment] = []

    # —— 1. 编号锚定 ——
    # 同一 number 可能对应多条(如 4.1、4.2 下各有 (1)(2)(3)(4)),
    # 按 (number, level) 分组后,同组按出现顺序依次配对。
    # level 约束避免「(1)」(level 3)与「1.」(level 2)误配。
    w_by_key: dict[tuple[str, int], list[Clause]] = {}
    for c in word_clauses:
        if c.number:
            w_by_key.setdefault((c.number, c.level), []).append(c)
    p_by_key: dict[tuple[str, int], list[Clause]] = {}
    for c in pdf_clauses:
        if c.number:
            p_by_key.setdefault((c.number, c.level), []).append(c)
    for key, p_list in p_by_key.items():
        w_list = w_by_key.get(key, [])
        for wc, pc in zip(w_list, p_list):
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

    # —— 2. 字段名锚定 ——
    # 两端 field_key 非空且未配对的 clause,按归一化 key 配对。
    # 同一 key 多条(如甲方:首部一条 + 签字页一条)按出现顺序依次配对。
    w_fields = _group_by_field(
        [c for c in word_clauses if c.field_key and c.clause_id not in matched_w]
    )
    p_fields = _group_by_field(
        [c for c in pdf_clauses if c.field_key and c.clause_id not in matched_p]
    )
    for key, w_list in w_fields.items():
        p_list = p_fields.get(key, [])
        for wc, pc in zip(w_list, p_list):
            alignments.append(
                Alignment(
                    word_clause_id=wc.clause_id,
                    pdf_clause_id=pc.clause_id,
                    match_type="field",
                    similarity=1.0,
                )
            )
            matched_w.add(wc.clause_id)
            matched_p.add(pc.clause_id)

    # —— 3. 语义向量匹配(邻域窗口优先 + 全局回退)——
    rem_w = [c for c in word_clauses if c.clause_id not in matched_w]
    rem_p = [c for c in pdf_clauses if c.clause_id not in matched_p]
    consumed_w: set[str] = set()
    if rem_w and rem_p:
        # 两端合并为一次批量 embedding RPC，远程引擎下由 2 次降为 1 次。
        all_vecs = _embed_clauses(rem_w + rem_p, embed)
        w_vecs = all_vecs[:len(rem_w)]
        p_vecs = all_vecs[len(rem_w):]

        for pi, pc in enumerate(rem_p):
            pv = p_vecs[pi]
            # 第一轮:邻域窗口内找最佳
            best, best_sim = _best_in_window(
                rem_w, w_vecs, pv, consumed_w, embed, pi, _NEIGHBORHOOD
            )
            # 第二轮:窗口内无达标,回退全局
            if best is None or best_sim < threshold:
                g_best, g_sim = _best_global(rem_w, w_vecs, pv, consumed_w, embed)
                if g_best is not None and g_sim > best_sim:
                    best, best_sim = g_best, g_sim

            if best is not None and best_sim >= threshold:
                alignments.append(
                    Alignment(
                        word_clause_id=best.clause_id,
                        pdf_clause_id=pc.clause_id,
                        match_type="semantic",
                        similarity=best_sim,
                    )
                )
                consumed_w.add(best.clause_id)
            else:
                alignments.append(
                    Alignment(
                        word_clause_id=None,
                        pdf_clause_id=pc.clause_id,
                        match_type="unmatched",
                        similarity=max(best_sim, 0.0),
                    )
                )
    elif rem_p:
        # word 侧已全部配对,剩余 pdf 条款 → added
        for pc in rem_p:
            alignments.append(
                Alignment(
                    word_clause_id=None,
                    pdf_clause_id=pc.clause_id,
                    match_type="unmatched",
                    similarity=0.0,
                )
            )

    # —— 4. word 剩余 → deleted ——
    for wc in rem_w:
        if wc.clause_id not in consumed_w:
            alignments.append(
                Alignment(
                    word_clause_id=wc.clause_id,
                    pdf_clause_id=None,
                    match_type="unmatched",
                    similarity=0.0,
                )
            )

    # —— 对齐结果统计(诊断「疑似切分边界差异」unmatched 多寡的关键信号)——
    type_counts = Counter(a.match_type for a in alignments)
    unmatched = type_counts.get("unmatched", 0)
    # 区分 added(pdf 独有)与 deleted(word 缺失)
    added = sum(1 for a in alignments if a.match_type == "unmatched" and not a.word_clause_id)
    deleted = unmatched - added
    logger.info(
        "align result number=%s field=%s semantic=%s unmatched=%s (added=%s deleted=%s) threshold=%.2f",
        type_counts.get("number", 0),
        type_counts.get("field", 0),
        type_counts.get("semantic", 0),
        unmatched, added, deleted,
        threshold,
    )

    return alignments


# —— 工具函数 ——
def _group_by_field(clauses: list[Clause]) -> dict[str, list[Clause]]:
    """按 field_key 分组,保持出现顺序(用于同 key 多条按序配对)。"""
    groups: dict[str, list[Clause]] = {}
    for c in clauses:
        groups.setdefault(c.field_key, []).append(c)
    return groups


def _embed_clauses(clauses: list[Clause], embed) -> list:
    """批量 embed(若引擎支持 embed_batch),否则逐条。返回与 clauses 对齐的向量列表。"""
    texts = [c.text for c in clauses]
    batch = getattr(embed, "embed_batch", None)
    if callable(batch):
        return batch(texts)
    return [embed.embed(t) for t in texts]


def _best_in_window(
    rem_w: list[Clause],
    w_vecs: list,
    pv,
    consumed_w: set[str],
    embed,
    pi: int,
    window: int,
) -> tuple[Clause | None, float]:
    """在邻域窗口(按 pdf 顺序位置 pi 映射到 word 顺序附近)内找最佳。"""
    best: Clause | None = None
    best_sim = -1.0
    lo = max(0, pi - window)
    hi = min(len(rem_w), pi + window + 1)
    for wi in range(lo, hi):
        wc = rem_w[wi]
        if wc.clause_id in consumed_w:
            continue
        s = embed.similarity(pv, w_vecs[wi])
        if s > best_sim:
            best, best_sim = wc, s
    return best, best_sim


def _best_global(
    rem_w: list[Clause], w_vecs: list, pv, consumed_w: set[str], embed
) -> tuple[Clause | None, float]:
    best: Clause | None = None
    best_sim = -1.0
    for wi, wc in enumerate(rem_w):
        if wc.clause_id in consumed_w:
            continue
        s = embed.similarity(pv, w_vecs[wi])
        if s > best_sim:
            best, best_sim = wc, s
    return best, best_sim
