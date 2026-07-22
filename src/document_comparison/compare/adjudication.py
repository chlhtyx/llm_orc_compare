"""条款变化裁决：变化、严重度与证据可信度的单一业务归属。"""
from __future__ import annotations

import re
from dataclasses import dataclass

from ..models import (
    ChangeVerdict,
    Clause,
    DiffSegment,
    DiffStatus,
    EvidenceConfidence,
    KeyElement,
    KeyElementKind,
    RiskLevel,
    TableStructure,
)
from .diff import char_diff, describe_table_change, table_diff
from .elements import (
    canonicalize_contract_text,
    elements_changed,
    extract_key_elements,
    reviewable_formatting_change,
    table_elements_changed,
)
from .risk import classify_diff, max_risk


@dataclass(frozen=True)
class ClauseAdjudication:
    status: DiffStatus
    risk_level: RiskLevel
    reasons: list[str]
    segments: list[DiffSegment]
    key_elements: list[KeyElement]
    verdict: ChangeVerdict
    confidence: EvidenceConfidence


_PROTECTED_FIELD_KINDS: dict[str, KeyElementKind] = {
    "甲方": "party",
    "乙方": "party",
    "供方": "party",
    "需方": "party",
    "出租方": "party",
    "承租方": "party",
    "买方": "party",
    "卖方": "party",
    "账号": "account",
    "统一社会信用代码": "identifier",
    "身份证号": "identifier",
    "法定代表人": "identifier",
    "负责人": "identifier",
    "账户名": "identifier",
    "开户行": "identifier",
    "地址": "identifier",
    "电话": "identifier",
    "传真": "identifier",
    "邮编": "identifier",
    "邮箱": "identifier",
    "日期": "date",
}

_CONFUSABLE_GROUPS = [set("0Oo〇"), set("1Il|"), set("5Ss"), set("8Bb")]


def _same_confusable_group(left: str, right: str) -> bool:
    return any(left in group and right in group for group in _CONFUSABLE_GROUPS)


def looks_like_ocr_confusion(word_text: str, pdf_text: str) -> bool:
    """识别少量等长 OCR 易混字符；只能触发复核，不能证明两端一致。"""
    left = re.sub(r"\s+", "", word_text)
    right = re.sub(r"\s+", "", pdf_text)
    if len(left) != len(right):
        return False
    differences = [(a, b) for a, b in zip(left, right) if a != b]
    return bool(differences) and len(differences) <= 3 and all(
        _same_confusable_group(a, b) for a, b in differences
    )


# 检测一端是另一端的严格前缀时，尾段的最大字符占比。
# 超过该比例即视为实质新增内容，而非切分粘连的签章/抬头碎片。
_SEGMENTATION_FRAGMENT_MAX_RATIO = 0.35


def looks_like_segmentation_artifact(
    word_text: str, pdf_text: str
) -> tuple[bool, str]:
    """识别切分边界不一致导致的「一端是另一端 + 短尾段」伪差异。

    合同正文末尾的签章/抬头/落款（如「XX市恒信商贸有限公司」）在 PDF 抽取时
    常因无编号/字段锚点而被拼到上一条款末尾，使该条款被判 modified。这里只识别
    *一端去掉空白后是另一端的严格前缀*，且尾段占比低于阈值的情形，把结论降为
    待复核而非确证变化。两侧表格差异不在此降级（由调用方保证 table_change_reason
    已处理）。返回 (命中, 尾段文本)。
    """
    left = re.sub(r"\s+", "", word_text)
    right = re.sub(r"\s+", "", pdf_text)
    if not left or not right or left == right:
        return False, ""
    # 一端是另一端的严格前缀 → 尾段即为差异
    if left.startswith(right):
        tail = left[len(right):]
        base_len = len(left)
    elif right.startswith(left):
        tail = right[len(left):]
        base_len = len(right)
    else:
        return False, ""
    # 尾段占比过大 → 视为实质内容，不降级
    if len(tail) / base_len > _SEGMENTATION_FRAGMENT_MAX_RATIO:
        return False, ""
    return True, tail


def _protected_field_change(word_clause: Clause, pdf_clause: Clause) -> KeyElement | None:
    field_key = word_clause.field_key or pdf_clause.field_key
    kind = _PROTECTED_FIELD_KINDS.get(field_key)
    if kind is None:
        return None
    if canonicalize_contract_text(word_clause.text) == canonicalize_contract_text(
        pdf_clause.text
    ):
        return None
    return KeyElement(
        kind=kind,
        word_value=word_clause.text,
        pdf_value=pdf_clause.text,
        changed=True,
    )


def _table_text(tables: list[TableStructure]) -> str:
    """保留行列边界地串行化表格，供格式差异分型使用。"""
    table_parts: list[str] = []
    for table in tables:
        rows = [table.headers, *table.rows]
        table_parts.append("␞".join("␟".join(row) for row in rows))
    return "␝".join(table_parts)


def adjudicate_clause_pair(
    word_clause: Clause,
    pdf_clause: Clause,
    *,
    similarity: float,
    sim_identical: float,
    sim_modified: float,
) -> ClauseAdjudication:
    """对一对已对齐条款裁决；语义相似度不得产生 clean 结论。"""
    word_text = word_clause.text
    pdf_text = pdf_clause.text
    canonical_word = canonicalize_contract_text(word_text)
    canonical_pdf = canonicalize_contract_text(pdf_text)

    key_elements = elements_changed(
        extract_key_elements(word_text), extract_key_elements(pdf_text)
    )
    key_elements.extend(table_elements_changed(word_clause.tables, pdf_clause.tables))
    protected = _protected_field_change(word_clause, pdf_clause)
    if protected is not None:
        key_elements.append(protected)

    segments = char_diff(word_text, pdf_text)
    table_change_reason = describe_table_change(word_clause.tables, pdf_clause.tables)
    for table_index in range(min(len(word_clause.tables), len(pdf_clause.tables))):
        word_table = word_clause.tables[table_index]
        pdf_table = pdf_clause.tables[table_index]
        if word_table.headers == pdf_table.headers and word_table.rows == pdf_table.rows:
            continue
        segments.extend(table_diff(word_table, pdf_table))

    status, risk, reasons = classify_diff(
        word_text=canonical_word,
        pdf_text=canonical_pdf,
        similarity=similarity,
        key_elements=key_elements,
        sim_identical=sim_identical,
        sim_modified=sim_modified,
        table_change_reason=table_change_reason,
    )
    if status == "identical":
        return ClauseAdjudication(
            status=status,
            risk_level=risk,
            reasons=reasons,
            segments=segments,
            key_elements=key_elements,
            verdict="clean",
            confidence="high",
        )

    verdict: ChangeVerdict = "changed"
    confidence: EvidenceConfidence = "high"
    if looks_like_ocr_confusion(word_text, pdf_text):
        verdict = "needs_review"
        confidence = "low"
        reasons = [*reasons, "疑似 OCR 易混字符，需核对原始图像"]
    else:
        body_changed = canonical_word != canonical_pdf
        body_formatting = reviewable_formatting_change(word_text, pdf_text)
        table_changed = bool(table_change_reason)
        table_formatting = (
            reviewable_formatting_change(
                _table_text(word_clause.tables),
                _table_text(pdf_clause.tables),
            )
            if table_changed
            else None
        )
        formatting_kind = None
        if (
            (not body_changed or body_formatting is not None)
            and (not table_changed or table_formatting is not None)
        ):
            formatting_kinds = {body_formatting, table_formatting} - {None}
            if "punctuation" in formatting_kinds:
                formatting_kind = "punctuation"
            elif "spacing" in formatting_kinds:
                formatting_kind = "spacing"
        if formatting_kind == "spacing":
            verdict = "needs_review"
            confidence = "low"
            reasons = [*reasons, "仅存在可能影响分词的空格差异，需核对原始图像"]
        elif formatting_kind == "punctuation":
            verdict = "needs_review"
            confidence = "low"
            reasons = [*reasons, "仅存在低证据标点或标点伴随空格差异，需核对原始图像"]
        elif (
            # 切分粘连：一端是另一端 + 短尾段（如签章/抬头被拼到条款末尾）。
            # 仅在无表格结构差异时降级，避免掩盖真实的表格增删。
            not table_change_reason
            and looks_like_segmentation_artifact(word_text, pdf_text)[0]
        ):
            verdict = "needs_review"
            confidence = "low"
            reasons = [*reasons, "疑似切分边界不一致导致的尾段粘连，需核对原始图像"]

    return ClauseAdjudication(
        status=status,
        risk_level=risk,
        reasons=reasons,
        segments=segments,
        key_elements=key_elements,
        verdict=verdict,
        confidence=confidence,
    )


def apply_judge_advice(
    rule_risk: RiskLevel,
    rule_reasons: list[str],
    judge_risk: RiskLevel,
    judge_reasons: list[str],
) -> tuple[RiskLevel, list[str]]:
    """LLM 仅能升级严重度或追加解释，不能降低确定性规则下限。"""
    final_risk = max_risk([rule_risk, judge_risk])
    reasons = [*rule_reasons, *judge_reasons]
    if final_risk == rule_risk and judge_risk != rule_risk:
        reasons.append("LLM 建议未改变确定性规则下限")
    return final_risk, list(dict.fromkeys(reasons))
