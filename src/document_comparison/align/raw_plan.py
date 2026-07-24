"""RawBlock/RawSpan 联合分段对齐。

该层消费 DOCX 段落与 PDF OCR 原始块，而不是已经固定边界的 Clause。LLM
只生成字符区间分组；代码负责校验计划、物化 Clause、计算相似度，最终变化仍
由 compare/report 的确定性规则裁决。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable

from ..models import (
    Alignment,
    Block,
    Clause,
    DocType,
    RawAlignmentBlock,
    RawAlignmentPlan,
    RawItem,
    RawSpan,
    TableStructure,
)
from ..structure.clause import detect_field_key, detect_number, detect_section_key

logger = logging.getLogger(__name__)

RawPlanResolver = Callable[
    [list[RawAlignmentBlock], list[RawAlignmentBlock]],
    RawAlignmentPlan | dict | None,
]


@dataclass(frozen=True)
class RawAlignmentResult:
    word_by: dict[str, Clause]
    pdf_by: dict[str, Clause]
    alignments: list[Alignment]


def align_raw_items(
    word_items: list[RawItem],
    pdf_items: list[RawItem],
    *,
    embed,
    planner: RawPlanResolver,
) -> RawAlignmentResult | None:
    """联合规划原始块；任何非法或不完整计划都返回 ``None`` 供调用方回退。"""
    word_blocks = _block_views(word_items, "w")
    pdf_blocks = _block_views(pdf_items, "p")
    try:
        raw_plan = planner(word_blocks, pdf_blocks)
        if raw_plan is None:
            return None
        plan = (
            raw_plan
            if isinstance(raw_plan, RawAlignmentPlan)
            else RawAlignmentPlan.model_validate(raw_plan)
        )
        _validate_plan(plan, word_blocks, pdf_blocks, word_items, pdf_items)
        return _materialize_plan(plan, word_items, pdf_items, embed)
    except Exception as exc:  # noqa: BLE001 - 联合计划失败必须安全回退
        logger.warning(
            "raw alignment plan rejected, fallback to clause alignment: %s",
            exc,
        )
        return None


def _block_views(items: list[RawItem], prefix: str) -> list[RawAlignmentBlock]:
    return [
        RawAlignmentBlock(
            block_id=f"{prefix}-{index}",
            text=item.text,
            kind=item.kind,
            page_index=item.page_index,
            bbox=list(item.bbox),
        )
        for index, item in enumerate(items, start=1)
        if item.text
    ]


def _validate_plan(
    plan: RawAlignmentPlan,
    word_blocks: list[RawAlignmentBlock],
    pdf_blocks: list[RawAlignmentBlock],
    word_items: list[RawItem],
    pdf_items: list[RawItem],
) -> None:
    if not plan.groups:
        raise ValueError("empty alignment plan")
    if len(plan.groups) > len(word_blocks) + len(pdf_blocks) + 100:
        raise ValueError("too many alignment groups")
    _validate_side(
        [group.word_spans for group in plan.groups],
        word_blocks,
        word_items,
        "word",
    )
    _validate_side(
        [group.pdf_spans for group in plan.groups],
        pdf_blocks,
        pdf_items,
        "pdf",
    )
    for group in plan.groups:
        if group.word_spans and group.pdf_spans and group.confidence < 0.75:
            raise ValueError("paired group confidence below 0.75")
        if len(group.word_spans) > 8 or len(group.pdf_spans) > 8:
            raise ValueError("alignment group contains too many spans")


def _validate_side(
    grouped_spans: list[list[RawSpan]],
    blocks: list[RawAlignmentBlock],
    items: list[RawItem],
    side: str,
) -> None:
    block_index = {block.block_id: index for index, block in enumerate(blocks)}
    text_by_id = {block.block_id: block.text for block in blocks}
    item_by_id = {
        block.block_id: items[int(block.block_id.split("-", 1)[1]) - 1]
        for block in blocks
    }
    intervals: dict[str, list[tuple[int, int]]] = {
        block.block_id: [] for block in blocks
    }
    previous_position: tuple[int, int] | None = None
    for spans in grouped_spans:
        for span in spans:
            if span.block_id not in block_index:
                raise ValueError(f"{side} references unknown block {span.block_id}")
            text = text_by_id[span.block_id]
            if span.start >= span.end or span.end > len(text):
                raise ValueError(f"{side} span outside block bounds")
            if item_by_id[span.block_id].kind == "table" and (
                span.start != 0 or span.end != len(text)
            ):
                raise ValueError(f"{side} table block cannot be partially split")
            position = (block_index[span.block_id], span.start)
            if previous_position is not None and position < previous_position:
                raise ValueError(f"{side} spans are not monotonic")
            previous_position = position
            intervals[span.block_id].append((span.start, span.end))

    # 全覆盖且无重叠：模型不能通过遗漏文本制造 clean 结论。
    for block in blocks:
        cursor = 0
        for start, end in sorted(intervals[block.block_id]):
            if start != cursor:
                raise ValueError(f"{side} block {block.block_id} is not exactly covered")
            cursor = end
        if cursor != len(block.text):
            raise ValueError(f"{side} block {block.block_id} is not fully covered")


def _materialize_plan(
    plan: RawAlignmentPlan,
    word_items: list[RawItem],
    pdf_items: list[RawItem],
    embed,
) -> RawAlignmentResult:
    word_by: dict[str, Clause] = {}
    pdf_by: dict[str, Clause] = {}
    alignments: list[Alignment] = []
    paired: list[tuple[Alignment, Clause, Clause]] = []

    for index, group in enumerate(plan.groups, start=1):
        word_clause = _clause_from_spans(
            group.word_spans, word_items, "word", f"word-plan-{index}"
        )
        pdf_clause = _clause_from_spans(
            group.pdf_spans, pdf_items, "pdf", f"pdf-plan-{index}"
        )
        if word_clause is not None:
            word_by[word_clause.clause_id] = word_clause
        if pdf_clause is not None:
            pdf_by[pdf_clause.clause_id] = pdf_clause

        if word_clause is None or pdf_clause is None:
            alignment = Alignment(
                word_clause_id=word_clause.clause_id if word_clause else None,
                pdf_clause_id=pdf_clause.clause_id if pdf_clause else None,
                match_type="unmatched",
                similarity=0.0,
                alignment_reason=_alignment_reason(group.reason),
            )
        else:
            alignment = Alignment(
                word_clause_id=word_clause.clause_id,
                pdf_clause_id=pdf_clause.clause_id,
                match_type="llm",
                similarity=0.0,
                alignment_reason=_alignment_reason(group.reason),
            )
            paired.append((alignment, word_clause, pdf_clause))
        alignments.append(alignment)

    if paired:
        texts = [text for _, wc, pc in paired for text in (wc.text, pc.text)]
        batch = getattr(embed, "embed_batch", None)
        vectors = (
            batch(texts)
            if callable(batch)
            else [embed.embed(text) for text in texts]
        )
        for pair_index, (alignment, _wc, _pc) in enumerate(paired):
            alignment.similarity = embed.similarity(
                vectors[pair_index * 2],
                vectors[pair_index * 2 + 1],
            )

    return RawAlignmentResult(
        word_by=word_by,
        pdf_by=pdf_by,
        alignments=alignments,
    )


def _alignment_reason(reason: str) -> str:
    bounded = reason if len(reason) <= 200 else f"{reason[:200]}…"
    return f"raw-span plan: {bounded}".strip()


def _clause_from_spans(
    spans: list[RawSpan],
    items: list[RawItem],
    doc_type: DocType,
    clause_id: str,
) -> Clause | None:
    if not spans:
        return None
    pieces: list[str] = []
    evidence_blocks: list[Block] = []
    tables: list[TableStructure] = []
    previous_block_id = ""
    for span in spans:
        item_index = int(span.block_id.split("-", 1)[1]) - 1
        item = items[item_index]
        piece = item.text[span.start:span.end]
        if pieces and previous_block_id != span.block_id:
            pieces.append("\n")
        pieces.append(piece)
        previous_block_id = span.block_id
        if item.bbox:
            evidence_blocks.append(
                Block(
                    block_id=f"{span.block_id}:{span.start}-{span.end}",
                    page_index=item.page_index,
                    label=item.kind,
                    bbox=list(item.bbox),
                    content=piece,
                    table=(
                        item.table
                        if span.start == 0 and span.end == len(item.text)
                        else None
                    ),
                )
            )
        if (
            item.table is not None
            and span.start == 0
            and span.end == len(item.text)
        ):
            tables.append(item.table)

    text = "".join(pieces).strip()
    number = ""
    title = ""
    level = 0
    field_key = ""
    numbered = detect_number(text)
    if numbered:
        prefix, number, level = numbered
        body = text[len(prefix):].lstrip(" .、．)）:：").strip()
        title = body
        section = detect_section_key(body)
        if section:
            field_key = section[1]
    else:
        field = detect_field_key(text)
        if field:
            _prefix, field_key = field
            title = field_key
        else:
            section = detect_section_key(text)
            if section:
                title, field_key = section
                level = 1

    return Clause(
        clause_id=clause_id,
        doc_type=doc_type,
        level=level,
        number=number,
        title=title,
        text=text,
        blocks=evidence_blocks,
        tables=tables,
        field_key=field_key,
    )
