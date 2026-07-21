"""条款对齐:五层兜底策略(§5.4)。

1. 编号锚定:按 number 精确匹配(篡改通常改内容不改编号)。
2. 字段名锚定:首部/签字页等无编号键值块(甲方/乙方/地址/电话/日期 等)
   按 field_key 配对,解决两端切分边界不一致导致的误判 added/deleted。
   同一 field_key 多条时按出现顺序配对(甲方首部信息 + 甲方签字页是两条)。
3. 规范文本精确匹配:忽略排版空白后完全一致的条款直接配对。
4. 语义向量匹配:对未配对条款做全局单调最优匹配,阈值确认。
5. 仍未配对 → added(pdf 独有)/ deleted(word 缺失)。

> 顺序敏感:合同条款顺序稳定,语义匹配使用全局单调动态规划，避免逐条
> 贪心抢占后续候选，也禁止产生交叉配对。
"""
from __future__ import annotations

import logging
import re
from collections import Counter
from difflib import SequenceMatcher

from ..models import Alignment, Clause
from ..structure.normalize import normalize_text

logger = logging.getLogger(__name__)

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

    # —— 3. 规范文本精确匹配 ——
    # OCR/PDF 文本层可能把标题识别为「采 购 合 同」并在冒号后插入空格。
    # 这些排版空白不应依赖 embedding 阈值来消除。重复文本按文档顺序配对，
    # 与字段锚定一致，避免远处重复页眉导致不稳定匹配。
    w_exact = _group_by_text_key(
        [c for c in word_clauses if c.clause_id not in matched_w]
    )
    p_exact = _group_by_text_key(
        [c for c in pdf_clauses if c.clause_id not in matched_p]
    )
    for key, w_list in w_exact.items():
        p_list = p_exact.get(key, [])
        for wc, pc in zip(w_list, p_list):
            alignments.append(
                Alignment(
                    word_clause_id=wc.clause_id,
                    pdf_clause_id=pc.clause_id,
                    match_type="normalized_exact",
                    similarity=1.0,
                )
            )
            matched_w.add(wc.clause_id)
            matched_p.add(pc.clause_id)

    # —— 4. 语义向量匹配(全局单调最优)——
    rem_w = [c for c in word_clauses if c.clause_id not in matched_w]
    rem_p = [c for c in pdf_clauses if c.clause_id not in matched_p]
    consumed_w: set[str] = set()
    if rem_w and rem_p:
        # 两端合并为一次批量 embedding RPC，远程引擎下由 2 次降为 1 次。
        all_vecs = _embed_clauses(rem_w + rem_p, embed)
        w_vecs = all_vecs[:len(rem_w)]
        p_vecs = all_vecs[len(rem_w):]

        similarities = [
            [embed.similarity(p_vec, w_vec) for p_vec in p_vecs]
            for w_vec in w_vecs
        ]
        matches = _global_monotonic_matches(similarities, threshold)
        by_pdf = {
            pdf_index: (word_index, sim)
            for word_index, pdf_index, sim in matches
        }

        for pdf_index, pc in enumerate(rem_p):
            matched = by_pdf.get(pdf_index)
            if matched is not None:
                word_index, best_sim = matched
                best = rem_w[word_index]
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
                best_word_index = max(
                    range(len(rem_w)),
                    key=lambda word_index: similarities[word_index][pdf_index],
                )
                best_sim = similarities[best_word_index][pdf_index]
                alignments.append(
                    Alignment(
                        word_clause_id=None,
                        pdf_clause_id=pc.clause_id,
                        match_type="unmatched",
                        similarity=max(best_sim, 0.0),
                        alignment_reason=_unmatched_reason(
                            rem_w[best_word_index], pc, best_sim, threshold
                        ),
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
                    alignment_reason="Word 侧没有可供配对的条款",
                )
            )

    # —— 5. word 剩余 → deleted ——
    for word_index, wc in enumerate(rem_w):
        if wc.clause_id not in consumed_w:
            if rem_p:
                best_pdf_index = max(
                    range(len(rem_p)),
                    key=lambda pdf_index: similarities[word_index][pdf_index],
                )
                best_sim = similarities[word_index][best_pdf_index]
                reason = _unmatched_reason(
                    wc, rem_p[best_pdf_index], best_sim, threshold
                )
            else:
                best_sim = 0.0
                reason = "PDF 侧没有可供配对的条款"
            alignments.append(
                Alignment(
                    word_clause_id=wc.clause_id,
                    pdf_clause_id=None,
                    match_type="unmatched",
                    similarity=max(best_sim, 0.0),
                    alignment_reason=reason,
                )
            )

    # —— 对齐结果统计(诊断「疑似切分边界差异」unmatched 多寡的关键信号)——
    type_counts = Counter(a.match_type for a in alignments)
    unmatched = type_counts.get("unmatched", 0)
    # 区分 added(pdf 独有)与 deleted(word 缺失)
    added = sum(1 for a in alignments if a.match_type == "unmatched" and not a.word_clause_id)
    deleted = unmatched - added
    logger.info(
        "align result number=%s field=%s normalized_exact=%s semantic=%s unmatched=%s (added=%s deleted=%s) threshold=%.2f",
        type_counts.get("number", 0),
        type_counts.get("field", 0),
        type_counts.get("normalized_exact", 0),
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


def _unmatched_reason(
    word_clause: Clause,
    pdf_clause: Clause,
    similarity: float,
    threshold: float,
) -> str:
    """解释相近条款为何没有配成一对，并给出有界的字符差异线索。"""
    if similarity < threshold:
        reason = (
            f"最相近的另一侧条款语义相似度 {similarity:.3f}，"
            f"低于对齐阈值 {threshold:.3f}"
        )
    else:
        reason = (
            "存在达到阈值的候选，但未进入全局一对一单调最优配对"
            f"（相似度 {similarity:.3f}，阈值 {threshold:.3f}）"
        )
    hint = _text_difference_hint(word_clause.text, pdf_clause.text)
    return f"{reason}；主要字符差异：{hint}" if hint else reason


def _text_difference_hint(word_text: str, pdf_text: str) -> str:
    """忽略排版空白后，摘取少量 Word/PDF 独有字符用于解释未对齐。"""
    word = _text_alignment_key(word_text)
    pdf = _text_alignment_key(pdf_text)
    word_only: list[str] = []
    pdf_only: list[str] = []
    for tag, w_start, w_end, p_start, p_end in SequenceMatcher(
        None, word, pdf, autojunk=False
    ).get_opcodes():
        if tag in ("delete", "replace") and w_start != w_end:
            word_only.append(word[w_start:w_end])
        if tag in ("insert", "replace") and p_start != p_end:
            pdf_only.append(pdf[p_start:p_end])

    hints: list[str] = []
    if word_only:
        hints.append(f"Word 独有“{_bounded_snippets(word_only)}”")
    if pdf_only:
        hints.append(f"PDF 独有“{_bounded_snippets(pdf_only)}”")
    return "；".join(hints)


def _bounded_snippets(parts: list[str], limit: int = 40) -> str:
    """原因字段保持简短，避免把整份条款重复写入报告。"""
    value = "…".join(parts[:3])
    return value if len(value) <= limit else f"{value[:limit]}…"


def _text_alignment_key(text: str) -> str:
    """生成仅供条款对齐使用的键：统一 Unicode 并忽略所有排版空白。"""
    return re.sub(r"\s+", "", normalize_text(text))


def _group_by_text_key(clauses: list[Clause]) -> dict[str, list[Clause]]:
    """按规范文本分组；空文本不参与精确对齐。"""
    groups: dict[str, list[Clause]] = {}
    for clause in clauses:
        key = _text_alignment_key(clause.text)
        if key:
            groups.setdefault(key, []).append(clause)
    return groups


def _embed_clauses(clauses: list[Clause], embed) -> list:
    """批量 embed(若引擎支持 embed_batch),否则逐条。返回与 clauses 对齐的向量列表。"""
    texts = [c.text for c in clauses]
    batch = getattr(embed, "embed_batch", None)
    if callable(batch):
        return batch(texts)
    return [embed.embed(t) for t in texts]


def _global_monotonic_matches(
    similarities: list[list[float]], threshold: float
) -> list[tuple[int, int, float]]:
    """最大化总相似度的一对一单调匹配；低于阈值的边不可用。"""
    word_count = len(similarities)
    pdf_count = len(similarities[0]) if similarities else 0
    scores = [[0.0] * (pdf_count + 1) for _ in range(word_count + 1)]
    counts = [[0] * (pdf_count + 1) for _ in range(word_count + 1)]
    back = [[""] * (pdf_count + 1) for _ in range(word_count + 1)]

    for word_index in range(1, word_count + 1):
        back[word_index][0] = "skip_word"
    for pdf_index in range(1, pdf_count + 1):
        back[0][pdf_index] = "skip_pdf"

    def better(
        candidate_score: float,
        candidate_count: int,
        best_score: float,
        best_count: int,
    ) -> bool:
        return candidate_score > best_score + 1e-12 or (
            abs(candidate_score - best_score) <= 1e-12 and candidate_count > best_count
        )

    for word_index in range(1, word_count + 1):
        for pdf_index in range(1, pdf_count + 1):
            best_score = scores[word_index - 1][pdf_index]
            best_count = counts[word_index - 1][pdf_index]
            best_op = "skip_word"

            if better(
                scores[word_index][pdf_index - 1],
                counts[word_index][pdf_index - 1],
                best_score,
                best_count,
            ):
                best_score = scores[word_index][pdf_index - 1]
                best_count = counts[word_index][pdf_index - 1]
                best_op = "skip_pdf"

            similarity = similarities[word_index - 1][pdf_index - 1]
            if similarity >= threshold:
                match_score = scores[word_index - 1][pdf_index - 1] + similarity
                match_count = counts[word_index - 1][pdf_index - 1] + 1
                if better(match_score, match_count, best_score, best_count):
                    best_score = match_score
                    best_count = match_count
                    best_op = "match"

            scores[word_index][pdf_index] = best_score
            counts[word_index][pdf_index] = best_count
            back[word_index][pdf_index] = best_op

    matches: list[tuple[int, int, float]] = []
    word_index, pdf_index = word_count, pdf_count
    while word_index > 0 or pdf_index > 0:
        op = back[word_index][pdf_index]
        if op == "match":
            matches.append((
                word_index - 1,
                pdf_index - 1,
                similarities[word_index - 1][pdf_index - 1],
            ))
            word_index -= 1
            pdf_index -= 1
        elif op == "skip_word":
            word_index -= 1
        else:
            pdf_index -= 1
    matches.reverse()
    return matches
