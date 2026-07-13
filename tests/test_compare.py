"""比对与风险分级测试。"""
from document_comparison.compare.diff import char_diff
from document_comparison.compare.elements import (
    elements_changed,
    extract_key_elements,
)
from document_comparison.compare.risk import classify_diff, max_risk
from document_comparison.models import Clause, KeyElement
from document_comparison.report.builder import _unmatched_risk


def test_char_diff_replace():
    segs = char_diff("金额100元", "金额200元")
    ops = {(s.op, s.text) for s in segs}
    # 字符级 diff:最小编辑距离,"100"→"200" 拆为 1→2 + 00 不变
    assert ("delete", "1") in ops
    assert ("insert", "2") in ops


def test_extract_amount_and_date():
    e = extract_key_elements("金额为100万元,2026年6月30日前交付,比例5%")
    assert any("100" in v for v in e.get("amount", []))
    assert any("2026" in v for v in e.get("date", []))
    assert any("5%" in v for v in e.get("ratio", []))


def test_elements_changed_detects_amount_change():
    we = extract_key_elements("金额100万元")
    pe = extract_key_elements("金额200万元")
    changed = elements_changed(we, pe)
    amount = [c for c in changed if c.kind == "amount"][0]
    assert amount.changed is True


def test_classify_identical():
    status, risk, _ = classify_diff(
        word_text="甲方义务", pdf_text="甲方义务",
        similarity=1.0, key_elements=[],
        sim_identical=0.98, sim_modified=0.85,
    )
    assert status == "identical" and risk == "none"


def test_classify_key_element_change_is_high():
    ke = [KeyElement(kind="amount", word_value="100万元", pdf_value="200万元", changed=True)]
    status, risk, reasons = classify_diff(
        word_text="金额100万元", pdf_text="金额200万元",
        similarity=0.95, key_elements=ke,
        sim_identical=0.98, sim_modified=0.85,
    )
    assert status == "modified" and risk == "high" and reasons


def test_classify_semantic_drop_is_medium():
    status, risk, _ = classify_diff(
        word_text="甲方承担全部责任", pdf_text="乙方承担全部责任",
        similarity=0.5, key_elements=[],
        sim_identical=0.98, sim_modified=0.85,
    )
    assert status == "modified" and risk == "medium"


def test_max_risk():
    assert max_risk(["low", "high", "medium"]) == "high"
    assert max_risk([]) == "none"


def test_unmatched_numbered_clause_is_high_risk():
    """编号条款未配对 → 高风险(疑似真实新增/缺失)。"""
    c = Clause(clause_id="x", doc_type="pdf", number="3.2", text="某条款")
    risk, reason = _unmatched_risk(c)
    assert risk == "high"
    assert "3.2" in reason


def test_unmatched_field_clause_is_low_risk():
    """键值块未配对 → 低风险(切分边界差异,待人工核对)。"""
    c = Clause(clause_id="x", doc_type="pdf", field_key="甲方", text="XX公司")
    risk, reason = _unmatched_risk(c)
    assert risk == "low"
    assert "人工核对" in reason


def test_unmatched_plain_clause_is_low_risk():
    """无编号无字段的散落文本未配对 → 低风险。"""
    c = Clause(clause_id="x", doc_type="pdf", text="(以下无正文)")
    risk, _ = _unmatched_risk(c)
    assert risk == "low"
