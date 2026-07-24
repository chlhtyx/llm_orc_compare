"""条款对齐:保守锚点 + 分段单调匹配策略。

1. 编号/字段强锚点:唯一项直接配对；重复项仅配规范文本一致或唯一残项，
   避免一侧缺项后按顺序 zip 造成级联错位。
2. 规范文本精确匹配:忽略排版空白后完全一致的条款直接配对。
3. 分段语义匹配:在非交叉强锚点之间做全局单调 1↔1、1↔2、2↔1 匹配。
4. 可选 LLM 裁决:只处理有界候选中的近分歧义或拆并候选，不参与差异判定。
5. 仍未配对 → added(pdf 独有)/ deleted(word 缺失)。

> 字符级、字段级和表格级变化仍由确定性比较判定；LLM 不能撤销真实变化。
"""
from __future__ import annotations

import logging
import re
from collections import Counter
from difflib import SequenceMatcher
from heapq import nlargest
from typing import Callable

from ..models import Alignment, Clause
from ..structure.normalize import normalize_text
from .llm_resolver import AlignmentCandidate, AlignmentDecision

logger = logging.getLogger(__name__)

def align_clauses(
    word_clauses: list[Clause],
    pdf_clauses: list[Clause],
    embed,
    threshold: float,
    llm_resolver: Callable[
        [list[AlignmentCandidate]],
        list[AlignmentDecision] | list[dict],
    ] | None = None,
) -> list[Alignment]:
    matched_w: set[str] = set()
    matched_p: set[str] = set()
    alignments: list[Alignment] = []

    # —— 1. 编号锚定 ——
    # 同一 number 可能对应多条(如 4.1、4.2 下各有 (1)(2)(3)(4))。
    # 按 (number, level) 分组后保守配对，避免一侧漏项导致后续级联错位。
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
        for wc, pc in _safe_anchor_pairs(w_list, p_list):
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
    # 两端 field_key 非空且未配对的 clause,按归一化 key 保守配对。
    # 同一 key 多条(如甲方:首部一条 + 签字页一条)不再直接按顺序 zip。
    w_fields = _group_by_field(
        [c for c in word_clauses if c.field_key and c.clause_id not in matched_w]
    )
    p_fields = _group_by_field(
        [c for c in pdf_clauses if c.field_key and c.clause_id not in matched_p]
    )
    for key, w_list in w_fields.items():
        p_list = p_fields.get(key, [])
        for wc, pc in _safe_anchor_pairs(w_list, p_list):
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

    # —— 4. 可靠锚点之间分段进行语义/拆并匹配 ——
    # 先去除可能交叉的强锚点；否则移除锚点后再对全部剩余条款跑一次 DP，
    # 仍可能把锚点前的 Word 条款配到锚点后的 PDF 条款。
    alignments = _monotonic_anchor_chain(alignments, word_clauses, pdf_clauses)
    matched_w = {
        alignment.word_clause_id
        for alignment in alignments
        if alignment.word_clause_id
    }
    matched_p = {
        alignment.pdf_clause_id
        for alignment in alignments
        if alignment.pdf_clause_id
    }
    word_pos = {clause.clause_id: index for index, clause in enumerate(word_clauses)}
    pdf_pos = {clause.clause_id: index for index, clause in enumerate(pdf_clauses)}
    anchors = sorted(
        (
            word_pos[alignment.word_clause_id],
            pdf_pos[alignment.pdf_clause_id],
        )
        for alignment in alignments
        if alignment.word_clause_id and alignment.pdf_clause_id
    )
    boundaries = [(-1, -1), *anchors, (len(word_clauses), len(pdf_clauses))]
    for (left_word, left_pdf), (right_word, right_pdf) in zip(
        boundaries, boundaries[1:]
    ):
        word_segment = [
            clause
            for clause in word_clauses[left_word + 1:right_word]
            if clause.clause_id not in matched_w
        ]
        pdf_segment = [
            clause
            for clause in pdf_clauses[left_pdf + 1:right_pdf]
            if clause.clause_id not in matched_p
        ]
        alignments.extend(
            _align_semantic_segment(
                word_segment,
                pdf_segment,
                embed,
                threshold,
                llm_resolver=llm_resolver,
            )
        )

    # —— 对齐结果统计(诊断「疑似切分边界差异」unmatched 多寡的关键信号)——
    type_counts = Counter(a.match_type for a in alignments)
    unmatched = type_counts.get("unmatched", 0)
    # 区分 added(pdf 独有)与 deleted(word 缺失)
    added = sum(1 for a in alignments if a.match_type == "unmatched" and not a.word_clause_id)
    deleted = unmatched - added
    logger.info(
        "align result number=%s field=%s normalized_exact=%s semantic=%s llm=%s unmatched=%s (added=%s deleted=%s) threshold=%.2f",
        type_counts.get("number", 0),
        type_counts.get("field", 0),
        type_counts.get("normalized_exact", 0),
        type_counts.get("semantic", 0),
        type_counts.get("llm", 0),
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


def _safe_anchor_pairs(
    word_clauses: list[Clause],
    pdf_clauses: list[Clause],
) -> list[tuple[Clause, Clause]]:
    """保守配对重复强锚点，避免一侧漏项导致后续按序级联错位。

    唯一锚点仍直接配对，以便正文变化进入确定性差异裁决。重复锚点先配正文
    规范化后完全一致的项；若精确项移除后两侧各只剩一个，再安全配对该残项。
    其余歧义项留给后续全局单调语义匹配。
    """
    if len(word_clauses) == 1 and len(pdf_clauses) == 1:
        return [(word_clauses[0], pdf_clauses[0])]

    pdf_by_text: dict[str, list[Clause]] = {}
    for clause in pdf_clauses:
        pdf_by_text.setdefault(_text_alignment_key(clause.text), []).append(clause)

    pairs: list[tuple[Clause, Clause]] = []
    used_word: set[str] = set()
    used_pdf: set[str] = set()
    for word_clause in word_clauses:
        candidates = pdf_by_text.get(_text_alignment_key(word_clause.text), [])
        pdf_clause = next(
            (item for item in candidates if item.clause_id not in used_pdf),
            None,
        )
        if pdf_clause is None:
            continue
        pairs.append((word_clause, pdf_clause))
        used_word.add(word_clause.clause_id)
        used_pdf.add(pdf_clause.clause_id)

    remaining_word = [
        clause for clause in word_clauses if clause.clause_id not in used_word
    ]
    remaining_pdf = [
        clause for clause in pdf_clauses if clause.clause_id not in used_pdf
    ]
    if len(remaining_word) == 1 and len(remaining_pdf) == 1:
        pairs.append((remaining_word[0], remaining_pdf[0]))
    return pairs


def _monotonic_anchor_chain(
    alignments: list[Alignment],
    word_clauses: list[Clause],
    pdf_clauses: list[Clause],
) -> list[Alignment]:
    """保留不交叉的最长强锚点链，其他候选退回语义匹配。"""
    if len(alignments) < 2:
        return alignments
    word_pos = {clause.clause_id: index for index, clause in enumerate(word_clauses)}
    pdf_pos = {clause.clause_id: index for index, clause in enumerate(pdf_clauses)}
    ordered = sorted(
        alignments,
        key=lambda item: word_pos.get(item.word_clause_id or "", -1),
    )
    lengths = [1] * len(ordered)
    previous = [-1] * len(ordered)
    for current in range(len(ordered)):
        current_pdf = pdf_pos.get(ordered[current].pdf_clause_id or "", -1)
        for candidate in range(current):
            candidate_pdf = pdf_pos.get(
                ordered[candidate].pdf_clause_id or "", -1
            )
            if candidate_pdf < current_pdf and lengths[candidate] + 1 > lengths[current]:
                lengths[current] = lengths[candidate] + 1
                previous[current] = candidate
    cursor = max(range(len(ordered)), key=lambda index: lengths[index])
    kept_indices: set[int] = set()
    while cursor >= 0:
        kept_indices.add(cursor)
        cursor = previous[cursor]
    kept = [item for index, item in enumerate(ordered) if index in kept_indices]
    if len(kept) != len(alignments):
        logger.warning(
            "discard crossing strong anchors total=%s kept=%s",
            len(alignments),
            len(kept),
        )
    return kept


def _align_semantic_segment(
    word_clauses: list[Clause],
    pdf_clauses: list[Clause],
    embed,
    threshold: float,
    *,
    llm_resolver: Callable[
        [list[AlignmentCandidate]],
        list[AlignmentDecision] | list[dict],
    ] | None,
) -> list[Alignment]:
    """在两个可靠锚点之间完成单调 1↔1/1↔2/2↔1 匹配。"""
    if not word_clauses:
        return [
            Alignment(
                pdf_clause_id=clause.clause_id,
                match_type="unmatched",
                alignment_reason="Word 侧没有可供配对的条款",
            )
            for clause in pdf_clauses
        ]
    if not pdf_clauses:
        return [
            Alignment(
                word_clause_id=clause.clause_id,
                match_type="unmatched",
                alignment_reason="PDF 侧没有可供配对的条款",
            )
            for clause in word_clauses
        ]

    all_vectors = _embed_clauses(word_clauses + pdf_clauses, embed)
    word_vectors = all_vectors[:len(word_clauses)]
    pdf_vectors = all_vectors[len(word_clauses):]
    similarities = [
        [embed.similarity(pdf_vector, word_vector) for pdf_vector in pdf_vectors]
        for word_vector in word_vectors
    ]
    matches = _global_monotonic_group_matches(
        word_clauses,
        pdf_clauses,
        word_vectors,
        pdf_vectors,
        similarities,
        embed,
        threshold,
        llm_resolver=llm_resolver,
    )
    consumed_word: set[str] = set()
    consumed_pdf: set[str] = set()
    result: list[Alignment] = []
    for word_indices, pdf_indices, similarity, llm_reason in matches:
        word_ids = [word_clauses[index].clause_id for index in word_indices]
        pdf_ids = [pdf_clauses[index].clause_id for index in pdf_indices]
        result.append(
            Alignment(
                word_clause_id=word_ids[0],
                pdf_clause_id=pdf_ids[0],
                word_clause_ids=word_ids,
                pdf_clause_ids=pdf_ids,
                match_type="llm" if llm_reason else "semantic",
                similarity=similarity,
                alignment_reason=llm_reason,
            )
        )
        consumed_word.update(word_ids)
        consumed_pdf.update(pdf_ids)

    for pdf_index, pdf_clause in enumerate(pdf_clauses):
        if pdf_clause.clause_id in consumed_pdf:
            continue
        best_word_index = max(
            range(len(word_clauses)),
            key=lambda index: similarities[index][pdf_index],
        )
        similarity = similarities[best_word_index][pdf_index]
        result.append(
            Alignment(
                pdf_clause_id=pdf_clause.clause_id,
                match_type="unmatched",
                similarity=max(similarity, 0.0),
                alignment_reason=_unmatched_reason(
                    word_clauses[best_word_index],
                    pdf_clause,
                    similarity,
                    threshold,
                ),
            )
        )
    for word_index, word_clause in enumerate(word_clauses):
        if word_clause.clause_id in consumed_word:
            continue
        best_pdf_index = max(
            range(len(pdf_clauses)),
            key=lambda index: similarities[word_index][index],
        )
        similarity = similarities[word_index][best_pdf_index]
        result.append(
            Alignment(
                word_clause_id=word_clause.clause_id,
                match_type="unmatched",
                similarity=max(similarity, 0.0),
                alignment_reason=_unmatched_reason(
                    word_clause,
                    pdf_clauses[best_pdf_index],
                    similarity,
                    threshold,
                ),
            )
        )
    return result


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


def _global_monotonic_group_matches(
    word_clauses: list[Clause],
    pdf_clauses: list[Clause],
    word_vectors: list,
    pdf_vectors: list,
    similarities: list[list[float]],
    embed,
    threshold: float,
    *,
    llm_resolver: Callable[
        [list[AlignmentCandidate]],
        list[AlignmentDecision] | list[dict],
    ] | None = None,
) -> list[tuple[tuple[int, ...], tuple[int, ...], float, str]]:
    """支持 1↔1、1↔2、2↔1 的全局单调最优匹配。

    拆分/合并候选只使用相邻条款，避免跨章节拼接；低于阈值的边仍不可用。
    """
    word_count = len(word_clauses)
    pdf_count = len(pdf_clauses)
    word_group_vectors: list = []
    pdf_group_vectors: list = []
    group_texts = [
        "\n".join((word_clauses[index].text, word_clauses[index + 1].text))
        for index in range(max(0, word_count - 1))
    ]
    group_texts.extend(
        "\n".join((pdf_clauses[index].text, pdf_clauses[index + 1].text))
        for index in range(max(0, pdf_count - 1))
    )
    if group_texts:
        batch = getattr(embed, "embed_batch", None)
        group_vectors = (
            batch(group_texts)
            if callable(batch)
            else [embed.embed(text) for text in group_texts]
        )
        word_group_count = max(0, word_count - 1)
        word_group_vectors = group_vectors[:word_group_count]
        pdf_group_vectors = group_vectors[word_group_count:]

    one_to_two = [
        [
            embed.similarity(pdf_group_vectors[pdf_index], word_vectors[word_index])
            for pdf_index in range(max(0, pdf_count - 1))
        ]
        for word_index in range(word_count)
    ]
    two_to_one = [
        [
            embed.similarity(pdf_vectors[pdf_index], word_group_vectors[word_index])
            for pdf_index in range(pdf_count)
        ]
        for word_index in range(max(0, word_count - 1))
    ]

    relaxed_threshold = max(0.0, threshold - 0.08)
    EdgeKey = tuple[tuple[int, ...], tuple[int, ...]]

    def make_candidate(edge: EdgeKey, similarity: float) -> AlignmentCandidate:
        word_indices, pdf_indices = edge
        word_ids = tuple(word_clauses[index].clause_id for index in word_indices)
        pdf_ids = tuple(pdf_clauses[index].clause_id for index in pdf_indices)
        candidate_id = f"w:{'+'.join(word_ids)}|p:{'+'.join(pdf_ids)}"
        relation = (
            "one_to_one"
            if len(word_indices) == len(pdf_indices) == 1
            else "one_to_many"
            if len(word_indices) == 1
            else "many_to_one"
        )
        return AlignmentCandidate(
            candidate_id=candidate_id,
            word_clause_ids=word_ids,
            pdf_clause_ids=pdf_ids,
            similarity=similarity,
            relation=relation,
            word_text="\n".join(word_clauses[index].text for index in word_indices),
            pdf_text="\n".join(pdf_clauses[index].text for index in pdf_indices),
            word_parent_paths=tuple(
                tuple(word_clauses[index].parent_path) for index in word_indices
            ),
            pdf_parent_paths=tuple(
                tuple(pdf_clauses[index].parent_path) for index in pdf_indices
            ),
        )
    selected: dict[EdgeKey, AlignmentDecision] = {}
    if llm_resolver is not None:
        group_specs = nlargest(
            30,
            (
                (similarity, ((word_index,), (pdf_index, pdf_index + 1)))
                for word_index, row in enumerate(one_to_two)
                for pdf_index, similarity in enumerate(row)
                if similarity >= relaxed_threshold
            ),
            key=lambda item: item[0],
        )
        group_specs.extend(
            nlargest(
                30,
                (
                    (
                        similarity,
                        ((word_index, word_index + 1), (pdf_index,)),
                    )
                    for word_index, row in enumerate(two_to_one)
                    for pdf_index, similarity in enumerate(row)
                    if similarity >= relaxed_threshold
                ),
                key=lambda item: item[0],
            )
        )
        ambiguous_specs: dict[EdgeKey, float] = {
            edge: similarity for similarity, edge in group_specs
        }
        relevant_word_indices = {
            index
            for _similarity, (word_indices, _pdf_indices) in group_specs
            for index in word_indices
        }
        relevant_pdf_indices = {
            index
            for _similarity, (_word_indices, pdf_indices) in group_specs
            for index in pdf_indices
        }
        for word_index in relevant_word_indices:
            for pdf_index, similarity in nlargest(
                2,
                enumerate(similarities[word_index]),
                key=lambda item: item[1],
            ):
                if similarity >= relaxed_threshold:
                    ambiguous_specs[((word_index,), (pdf_index,))] = similarity
        for pdf_index in relevant_pdf_indices:
            for word_index, similarity in nlargest(
                2,
                (
                    (word_index, similarities[word_index][pdf_index])
                    for word_index in range(word_count)
                ),
                key=lambda item: item[1],
            ):
                if similarity >= relaxed_threshold:
                    ambiguous_specs[((word_index,), (pdf_index,))] = similarity
        for word_index, row in enumerate(similarities):
            ranked = nlargest(
                2,
                enumerate(row),
                key=lambda item: item[1],
            )
            if (
                len(ranked) >= 2
                and ranked[0][1] >= relaxed_threshold
                and ranked[0][1] - ranked[1][1] <= 0.05
            ):
                for pdf_index, similarity in ranked:
                    ambiguous_specs[((word_index,), (pdf_index,))] = similarity
        for pdf_index in range(pdf_count):
            ranked = nlargest(
                2,
                (
                    (word_index, similarities[word_index][pdf_index])
                    for word_index in range(word_count)
                ),
                key=lambda item: item[1],
            )
            if (
                len(ranked) >= 2
                and ranked[0][1] >= relaxed_threshold
                and ranked[0][1] - ranked[1][1] <= 0.05
            ):
                for word_index, similarity in ranked:
                    ambiguous_specs[((word_index,), (pdf_index,))] = similarity

        # 只在确有拆并或近似并列时构造有界候选对象；普通明确 1↔1 不调 LLM。
        bounded_specs = nlargest(
            60,
            ambiguous_specs.items(),
            key=lambda item: item[1],
        )
        bounded_candidates = [
            make_candidate(edge, similarity)
            for edge, similarity in bounded_specs
        ]
        candidate_edges = {
            candidate.candidate_id: edge
            for (edge, _similarity), candidate in zip(
                bounded_specs, bounded_candidates
            )
        }
        candidate_by_id = {
            candidate.candidate_id: candidate
            for candidate in bounded_candidates
        }
    else:
        bounded_candidates = []
        candidate_edges = {}
        candidate_by_id = {}

    if bounded_candidates:
        try:
            decisions = llm_resolver(bounded_candidates)
        except Exception as exc:  # noqa: BLE001
            logger.warning("llm alignment resolver failed, fallback to semantic: %s", exc)
            decisions = []
        for value in decisions:
            try:
                decision = (
                    value
                    if isinstance(value, AlignmentDecision)
                    else AlignmentDecision(
                        candidate_id=str(value["candidate_id"]),
                        confidence=float(value["confidence"]),
                        reason=str(value.get("reason", "")),
                    )
                )
            except (KeyError, TypeError, ValueError):
                logger.warning("ignore invalid llm alignment decision=%r", value)
                continue
            candidate = candidate_by_id.get(decision.candidate_id)
            if (
                candidate is None
                or candidate.similarity < relaxed_threshold
                or not 0.75 <= decision.confidence <= 1.0
            ):
                logger.warning(
                    "ignore unsafe llm alignment decision candidate=%s confidence=%s",
                    decision.candidate_id,
                    decision.confidence,
                )
                continue
            selected[candidate_edges[decision.candidate_id]] = decision

    scores = [[0.0] * (pdf_count + 1) for _ in range(word_count + 1)]
    counts = [[0] * (pdf_count + 1) for _ in range(word_count + 1)]
    back: list[list[tuple[str, float, str] | None]] = [
        [None] * (pdf_count + 1) for _ in range(word_count + 1)
    ]
    for word_index in range(1, word_count + 1):
        back[word_index][0] = ("skip_word", 0.0, "")
    for pdf_index in range(1, pdf_count + 1):
        back[0][pdf_index] = ("skip_pdf", 0.0, "")

    def better(
        candidate_score: float,
        candidate_count: int,
        best_score: float,
        best_count: int,
    ) -> bool:
        return candidate_score > best_score + 1e-12 or (
            abs(candidate_score - best_score) <= 1e-12
            and candidate_count > best_count
        )

    for word_index in range(1, word_count + 1):
        for pdf_index in range(1, pdf_count + 1):
            best_score = scores[word_index - 1][pdf_index]
            best_count = counts[word_index - 1][pdf_index]
            best_op: tuple[str, float, str] = ("skip_word", 0.0, "")

            choices: list[tuple[str, int, int, float, EdgeKey | None]] = [
                ("skip_pdf", word_index, pdf_index - 1, 0.0, None),
                (
                    "match",
                    word_index - 1,
                    pdf_index - 1,
                    similarities[word_index - 1][pdf_index - 1],
                    ((word_index - 1,), (pdf_index - 1,)),
                ),
            ]
            if pdf_index >= 2:
                choices.append(
                    (
                        "match_1_2",
                        word_index - 1,
                        pdf_index - 2,
                        one_to_two[word_index - 1][pdf_index - 2],
                        (
                            (word_index - 1,),
                            (pdf_index - 2, pdf_index - 1),
                        ),
                    )
                )
            if word_index >= 2:
                choices.append(
                    (
                        "match_2_1",
                        word_index - 2,
                        pdf_index - 1,
                        two_to_one[word_index - 2][pdf_index - 1],
                        (
                            (word_index - 2, word_index - 1),
                            (pdf_index - 1,),
                        ),
                    )
                )

            for op, prev_word, prev_pdf, similarity, edge in choices:
                is_match = op.startswith("match")
                decision = selected.get(edge) if edge is not None else None
                if is_match and similarity < threshold and decision is None:
                    continue
                if op == "match_1_2" and decision is None:
                    component_best = max(
                        similarities[word_index - 1][pdf_index - 2],
                        similarities[word_index - 1][pdf_index - 1],
                    )
                    if similarity < component_best + 0.02:
                        continue
                if op == "match_2_1" and decision is None:
                    component_best = max(
                        similarities[word_index - 2][pdf_index - 1],
                        similarities[word_index - 1][pdf_index - 1],
                    )
                    if similarity < component_best + 0.02:
                        continue
                candidate_score = scores[prev_word][prev_pdf] + (
                    similarity
                    + (0.25 * decision.confidence if decision else 0.0)
                    if is_match
                    else 0.0
                )
                candidate_count = counts[prev_word][prev_pdf] + int(is_match)
                if better(
                    candidate_score,
                    candidate_count,
                    best_score,
                    best_count,
                ):
                    best_score = candidate_score
                    best_count = candidate_count
                    reason = (
                        f"LLM辅助对齐置信度 {decision.confidence:.3f}"
                        + (f"：{decision.reason}" if decision.reason else "")
                        if decision
                        else ""
                    )
                    best_op = (op, similarity, reason)

            scores[word_index][pdf_index] = best_score
            counts[word_index][pdf_index] = best_count
            back[word_index][pdf_index] = best_op

    matches: list[tuple[tuple[int, ...], tuple[int, ...], float, str]] = []
    word_index, pdf_index = word_count, pdf_count
    while word_index > 0 or pdf_index > 0:
        entry = back[word_index][pdf_index]
        op, similarity, reason = entry or ("skip_pdf", 0.0, "")
        if op == "match":
            matches.append(
                ((word_index - 1,), (pdf_index - 1,), similarity, reason)
            )
            word_index -= 1
            pdf_index -= 1
        elif op == "match_1_2":
            matches.append(
                (
                    (word_index - 1,),
                    (pdf_index - 2, pdf_index - 1),
                    similarity,
                    reason,
                )
            )
            word_index -= 1
            pdf_index -= 2
        elif op == "match_2_1":
            matches.append(
                (
                    (word_index - 2, word_index - 1),
                    (pdf_index - 1,),
                    similarity,
                    reason,
                )
            )
            word_index -= 2
            pdf_index -= 1
        elif op == "skip_word":
            word_index -= 1
        else:
            pdf_index -= 1
    matches.reverse()
    return matches
