"""比对报告组装:字符 diff + 风险分级 + 坐标归一化(§5.6、§6)。"""
from __future__ import annotations

import logging
import re
from collections import Counter
from pathlib import Path

import pymupdf

logger = logging.getLogger(__name__)

from ..compare.diff import char_diff, describe_table_change, table_diff
from ..compare.elements import (
    elements_changed,
    extract_key_elements,
    table_elements_changed,
)
from ..compare.judge import llm_judge_diff
from ..compare.risk import classify_diff, overall_risk_from_diffs
from ..models import (
    Alignment,
    Clause,
    Diff,
    DiffSegment,
    DiffStatus,
    KeyElement,
    PageMeta,
    PageRegion,
    TamperReport,
)
from ..structure.normalize import normalize_text


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
        x1, y1, x2, y2 = _clean_bbox(b.bbox[:4], w, h)
        if x2 <= x1 or y2 <= y1:
            continue
        regions.append(
            PageRegion(
                page_index=b.page_index,
                bbox=[x1 / w, y1 / h, x2 / w, y2 / h],
                shape="rect",
            )
        )
    return regions


def _clean_bbox(bbox: list[float], page_w: float, page_h: float) -> list[float]:
    x1, y1, x2, y2 = [float(v) for v in bbox[:4]]
    left, right = sorted((x1, x2))
    top, bottom = sorted((y1, y2))
    left = min(max(left, 0.0), page_w)
    right = min(max(right, 0.0), page_w)
    top = min(max(top, 0.0), page_h)
    bottom = min(max(bottom, 0.0), page_h)
    return [left, top, right, bottom]


def _unmatched_risk(c: Clause) -> tuple[str, str]:
    """未配对条款按来源分级(§8.3 调整)。

    - 编号条款未配对(number 非空):高风险,疑似真实新增/缺失。
    - 键值块未配对(field_key 非空):低风险,多为切分边界差异,待人工核对。
    - 无编号无字段的散落文本:低风险(OCR 噪声/页眉页脚)。
    返回 (risk_level, reason)。
    """
    if c.number:
        return ("high", f"待核件缺失/独有条款:编号 {c.number} 无配对")
    if c.field_key:
        return ("low", "疑似切分边界差异,待人工核对")
    return ("low", "疑似切分边界差异,待人工核对")


def _compact_clause_text(text: str) -> str:
    """条款边界判定用比较键：忽略排版空白，保留实际文字。"""
    return re.sub(r"\s+", "", normalize_text(text))


def _covered_boundary_alignments(
    alignments: list[Alignment],
    word_by: dict[str, Clause],
    pdf_by: dict[str, Clause],
) -> set[int]:
    """找出已被相邻配对条款完整覆盖的 unmatched 切分片段。

    只检查原文档顺序中相邻的已配对条款，避免因合同中远处重复短语
    而隐藏真实缺失。短于 4 个字符的片段不自动抑制。
    """
    word_ids = list(word_by)
    pdf_ids = list(pdf_by)
    word_pos = {clause_id: index for index, clause_id in enumerate(word_ids)}
    pdf_pos = {clause_id: index for index, clause_id in enumerate(pdf_ids)}
    paired = [
        alignment
        for alignment in alignments
        if alignment.match_type != "unmatched"
        and alignment.word_clause_id
        and alignment.pdf_clause_id
    ]
    covered: set[int] = set()

    for alignment_index, alignment in enumerate(alignments):
        if alignment.match_type != "unmatched":
            continue
        if alignment.word_clause_id and not alignment.pdf_clause_id:
            fragment = _compact_clause_text(word_by[alignment.word_clause_id].text)
            position = word_pos[alignment.word_clause_id]
            if len(fragment) >= 4 and any(
                abs(word_pos[pair.word_clause_id] - position) == 1
                and fragment in _compact_clause_text(pdf_by[pair.pdf_clause_id].text)
                for pair in paired
            ):
                covered.add(alignment_index)
        elif alignment.pdf_clause_id and not alignment.word_clause_id:
            fragment = _compact_clause_text(pdf_by[alignment.pdf_clause_id].text)
            position = pdf_pos[alignment.pdf_clause_id]
            if len(fragment) >= 4 and any(
                abs(pdf_pos[pair.pdf_clause_id] - position) == 1
                and fragment in _compact_clause_text(word_by[pair.word_clause_id].text)
                for pair in paired
            ):
                covered.add(alignment_index)
    return covered


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
    enable_llm_judge: bool = False,
) -> TamperReport:
    pmeta = {m.page_index: m for m in page_metas}
    diffs: list[Diff] = []
    unmatched: list[Diff] = []
    all_key_elements: list[KeyElement] = []
    levels: list[str] = []
    status_counts: Counter[str] = Counter()
    covered_boundary_alignments = _covered_boundary_alignments(
        alignments, word_by, pdf_by
    )

    # number 锚定时 Alignment.similarity=1 仅代表编号匹配，仍需计算正文相似度。
    # 将原来的每条两次 embed 合并为一次批量调用，避免远程服务产生 2N 次 RPC。
    number_pairs: list[tuple[int, Clause, Clause]] = []
    for idx, al in enumerate(alignments):
        if al.match_type != "number" or not al.word_clause_id or not al.pdf_clause_id:
            continue
        wc = word_by.get(al.word_clause_id)
        pc = pdf_by.get(al.pdf_clause_id)
        if wc and pc and wc.text != pc.text:
            number_pairs.append((idx, wc, pc))
    number_similarities: dict[int, float] = {}
    if number_pairs:
        texts = [text for _, wc, pc in number_pairs for text in (wc.text, pc.text)]
        batch = getattr(embed, "embed_batch", None)
        vectors = batch(texts) if callable(batch) else [embed.embed(text) for text in texts]
        for pair_pos, (alignment_idx, _wc, _pc) in enumerate(number_pairs):
            number_similarities[alignment_idx] = embed.similarity(
                vectors[pair_pos * 2], vectors[pair_pos * 2 + 1]
            )

    for idx, al in enumerate(alignments):
        if idx in covered_boundary_alignments:
            logger.info("suppress covered boundary fragment alignment=%s", idx)
            continue
        wc = word_by.get(al.word_clause_id) if al.word_clause_id else None
        pc = pdf_by.get(al.pdf_clause_id) if al.pdf_clause_id else None

        if al.match_type == "unmatched":
            if pc and not wc:  # added
                risk, reason = _unmatched_risk(pc)
                d = Diff(
                    alignment_id=f"al{idx}",
                    status="added",
                    risk_level=risk,
                    risk_reasons=[reason],
                    segments=[DiffSegment(op="insert", text=pc.text)],
                    number=pc.number,
                    title=pc.title,
                    page_regions=_normalize_regions(pc.blocks, pmeta),
                )
            else:  # deleted
                risk, reason = _unmatched_risk(wc) if wc else ("high", "待核件缺失条款")
                d = Diff(
                    alignment_id=f"al{idx}",
                    status="deleted",
                    risk_level=risk,
                    risk_reasons=[reason],
                    segments=[DiffSegment(op="delete", text=wc.text)] if wc else [],
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
        # 结构化表格要素:按单元格维度抽取并比对,补充到 key_elements
        tbl_ke = table_elements_changed(wc.tables, pc.tables)
        ke.extend(tbl_ke)
        all_key_elements.extend([e for e in ke if e.changed])
        segs = char_diff(wt, pt)
        table_change_reason = describe_table_change(wc.tables, pc.tables)
        # 表格单元格级 diff:两端均有结构化表格时追加,定位到具体单元格
        for tidx in range(min(len(wc.tables), len(pc.tables))):
            wt_tbl = wc.tables[tidx]
            pt_tbl = pc.tables[tidx]
            if wt_tbl.headers == pt_tbl.headers and wt_tbl.rows == pt_tbl.rows:
                continue  # 完全一致,不产生 diff
            segs.extend(table_diff(wt_tbl, pt_tbl))

        # 编号配对的相似度需实算(number 配对 similarity=1.0 不代表文本一致)
        if al.match_type == "number":
            # 完全相同文本无需调用 embedding；差异文本已在循环前批量计算。
            sim = 1.0 if wt == pt else number_similarities[idx]
        else:
            sim = al.similarity

        status, risk, reasons = classify_diff(
            word_text=wt,
            pdf_text=pt,
            similarity=sim,
            key_elements=ke,
            sim_identical=thresholds["identical"],
            sim_modified=thresholds["modified"],
            table_change_reason=table_change_reason,
        )
        status_counts[status] += 1
        if status == "identical":
            levels.append("none")
            continue  # 一致不入差异报告

        # LLM 复核:仅对 modified 条款调用,可修正风险等级(规则+LLM 结合)
        judged_by = "rule"
        if enable_llm_judge and status == "modified":
            risk, reasons = llm_judge_diff(wt, pt, segs, risk)
            judged_by = "llm"

        d = Diff(
            alignment_id=f"al{idx}",
            status=status,
            segments=segs,
            risk_level=risk,
            risk_reasons=reasons,
            judged_by=judged_by,
            page_regions=_normalize_regions(pc.blocks, pmeta),
            number=wc.number or pc.number,
            title=wc.title or pc.title,
        )
        diffs.append(d)
        levels.append(risk)

    overall = overall_risk_from_diffs(bool(alignments), levels)
    changed_elems = [e for e in all_key_elements if e.changed]
    summary = {
        "total_alignments": len(alignments) - len(covered_boundary_alignments),
        "status_counts": dict(status_counts),
        "risk_distribution": dict(Counter(levels)),
        "key_element_changes": len(changed_elems),
    }
    logger.info(
        "report built diffs=%s unmatched=%s overall=%s status=%s risk_dist=%s",
        len(diffs), len(unmatched), overall,
        dict(status_counts), dict(Counter(levels)),
    )
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


# ---- PDF 烧录：将高亮标注直接写入 PDF 页面 ----

_RISK_FILL = {
    "high": (1.0, 0.15, 0.15),     # 红
    "medium": (1.0, 0.75, 0.15),   # 黄
    "low": (0.2, 0.55, 1.0),       # 蓝
    "none": (0.6, 0.65, 0.7),      # 灰
}
_RISK_STROKE = {
    "high": (0.85, 0.0, 0.0),
    "medium": (0.85, 0.6, 0.0),
    "low": (0.1, 0.4, 0.9),
    "none": (0.4, 0.45, 0.5),
}


def burn_pdf(
    pdf_path: str | Path,
    report: TamperReport,
    output_path: str | Path,
) -> Path:
    """在源 PDF 页面上烧录差异标注矩形框,保存为新文件。

    标注来自 report.diffs 中所有带 page_regions 的条款。每个区域:
    - 按风险等级着色半透明矩形
    - 在矩形右上角附加简短编号标签

    返回输出文件路径。
    """
    doc = pymupdf.open(str(pdf_path))
    pmeta = {m.page_index: m for m in report.page_meta}
    # 逐页收集标注
    page_regions: dict[int, list[tuple[Diff, PageRegion]]] = {}
    for d in report.diffs:
        for r in d.page_regions:
            page_regions.setdefault(r.page_index, []).append((d, r))

    for pi in range(len(doc)):
        page = doc[pi]
        meta = pmeta.get(pi)
        if not meta:
            continue
        pw, ph = meta.pdf_width_pt, meta.pdf_height_pt
        if pw <= 0 or ph <= 0:
            continue
        regions = page_regions.get(pi, [])
        for d, r in regions:
            x1, y1, x2, y2 = r.bbox
            left = x1 * pw
            top = y1 * ph
            right = x2 * pw
            bottom = y2 * ph
            rect = pymupdf.Rect(left, top, right, bottom)
            color_fill = _RISK_FILL.get(d.risk_level, _RISK_FILL["none"])
            color_stroke = _RISK_STROKE.get(d.risk_level, _RISK_STROKE["none"])

            # 半透明填充
            annot = page.add_rect_annot(rect)
            annot.set_colors(fill=color_fill, stroke=color_stroke)
            annot.set_border(width=1.5)
            annot.set_opacity(0.35)
            annot.update()

            # 简短标签(编号 + 风险级缩写)
            label = d.number or d.alignment_id[-6:]
            risk_abbr = {"high": "高", "medium": "中", "low": "低", "none": "-"}.get(d.risk_level, "?")
            label_text = f"{label} [{risk_abbr}]"
            # 标签写在矩形右上角外侧
            label_rect = pymupdf.Rect(right + 2, top - 10, right + 80, top + 2)
            page.insert_textbox(
                label_rect,
                label_text,
                fontsize=7,
                color=color_stroke,
                fontname="china-s",
                align=0,
            )

        # 页脚标注
        if regions:
            footer = f"文档比对 · 本页 {len(regions)} 处差异"
            footer_rect = pymupdf.Rect(36, ph - 24, pw - 36, ph - 10)
            page.insert_textbox(
                footer_rect,
                footer,
                fontsize=8,
                color=(0.5, 0.5, 0.5),
                fontname="china-s",
                align=1,
            )

    doc.save(str(output_path), incremental=False, deflate=True)
    doc.close()
    return Path(output_path)
