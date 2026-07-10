"""比对报告组装:字符 diff + 风险分级 + 坐标归一化(§5.6、§6)。"""
from __future__ import annotations

from collections import Counter

from ..compare.diff import char_diff
from ..compare.elements import elements_changed, extract_key_elements
from ..compare.risk import classify_diff, overall_risk_from_diffs
from ..models import (
    Alignment,
    Clause,
    Diff,
    DiffStatus,
    KeyElement,
    PageMeta,
    PageRegion,
    TamperReport,
)


def _normalize_regions(blocks, pmeta: dict[int, PageMeta]) -> list[PageRegion]:
    """Block.bbox(pt)→ 归一化 PageRegion([0,1])。"""
    regions: list[PageRegion] = []
    for b in blocks:
        if len(b.bbox) < 4:
            continue
        meta = pmeta.get(b.page_index)
        if not meta or not (meta.pdf_width_pt and meta.pdf_height_pt):
            continue
        w, h = meta.pdf_width_pt, meta.pdf_height_pt
        x1, y1, x2, y2 = b.bbox[:4]
        regions.append(
            PageRegion(
                page_index=b.page_index,
                bbox=[x1 / w, y1 / h, x2 / w, y2 / h],
                shape="rect",
            )
        )
    return regions


def build_report(
    *,
    alignments: list[Alignment],
    word_by: dict[str, Clause],
    pdf_by: dict[str, Clause],
    embed,
    page_metas: list[PageMeta],
    thresholds: dict[str, float],
    source: str,
    target: str,
) -> TamperReport:
    pmeta = {m.page_index: m for m in page_metas}
    diffs: list[Diff] = []
    unmatched: list[Diff] = []
    all_key_elements: list[KeyElement] = []
    levels: list[str] = []
    status_counts: Counter[str] = Counter()

    for idx, al in enumerate(alignments):
        wc = word_by.get(al.word_clause_id) if al.word_clause_id else None
        pc = pdf_by.get(al.pdf_clause_id) if al.pdf_clause_id else None

        if al.match_type == "unmatched":
            if pc and not wc:  # added
                d = Diff(
                    alignment_id=f"al{idx}",
                    status="added",
                    risk_level="high",
                    risk_reasons=["待核件独有条款"],
                    number=pc.number,
                    title=pc.title,
                    page_regions=_normalize_regions(pc.blocks, pmeta),
                )
            else:  # deleted
                d = Diff(
                    alignment_id=f"al{idx}",
                    status="deleted",
                    risk_level="high",
                    risk_reasons=["待核件缺失条款"],
                    number=wc.number if wc else "",
                    title=wc.title if wc else "",
                )
            unmatched.append(d)
            status_counts[d.status] += 1
            levels.append(d.risk_level)
            continue

        if not (wc and pc):
            continue

        wt, pt = wc.text, pc.text
        ke = elements_changed(extract_key_elements(wt), extract_key_elements(pt))
        all_key_elements.extend([e for e in ke if e.changed])
        segs = char_diff(wt, pt)

        # 编号配对的相似度需实算(number 配对 similarity=1.0 不代表文本一致)
        if al.match_type == "number":
            sim = embed.similarity(embed.embed(wt), embed.embed(pt))
        else:
            sim = al.similarity

        status, risk, reasons = classify_diff(
            word_text=wt,
            pdf_text=pt,
            similarity=sim,
            key_elements=ke,
            sim_identical=thresholds["identical"],
            sim_modified=thresholds["modified"],
        )
        status_counts[status] += 1
        if status == "identical":
            levels.append("none")
            continue  # 一致不入差异报告

        d = Diff(
            alignment_id=f"al{idx}",
            status=status,
            segments=segs,
            risk_level=risk,
            risk_reasons=reasons,
            page_regions=_normalize_regions(pc.blocks, pmeta),
            number=wc.number or pc.number,
            title=wc.title or pc.title,
        )
        diffs.append(d)
        levels.append(risk)

    overall = overall_risk_from_diffs(bool(alignments), levels)
    changed_elems = [e for e in all_key_elements if e.changed]
    summary = {
        "total_alignments": len(alignments),
        "status_counts": dict(status_counts),
        "risk_distribution": dict(Counter(levels)),
        "key_element_changes": len(changed_elems),
    }
    return TamperReport(
        source=source,
        target=target,
        overall_risk=overall,
        summary=summary,
        diffs=diffs,
        key_elements=changed_elems,
        unmatched_clauses=unmatched,
        page_meta=page_metas,
    )
