"""比对与风险分级测试。"""
from document_comparison.compare.diff import char_diff, describe_table_change
from document_comparison.compare.elements import (
    elements_changed,
    extract_key_elements,
)
from document_comparison.compare.risk import classify_diff, max_risk
from document_comparison.models import Alignment, Clause, KeyElement, TableStructure
from document_comparison.report.builder import _unmatched_risk, build_report


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


class _AlmostIdenticalEmbedding:
    """模拟长表只缺一个普通文本时，整条语义相似度仍高于 identical 阈值。"""

    def embed_batch(self, texts):
        return texts

    def similarity(self, _left, _right):
        return 0.995


def _table_text(table: TableStructure) -> str:
    return "\n".join(
        " | ".join(row) for row in [table.headers, *table.rows]
    )


def test_missing_non_key_table_cell_is_reported_even_when_similarity_is_high():
    headers = ["序号", "产品名称", "数量", "备注"]
    rows = [[str(i), f"产品{i}", "100", "原装正品"] for i in range(1, 31)]
    word_table = TableStructure(headers=headers, rows=rows)
    pdf_rows = [list(row) for row in rows]
    pdf_rows[16][1] = ""  # PDF 缺失 DOCX 中的普通产品名称，不涉及金额等要素
    pdf_table = TableStructure(headers=headers, rows=pdf_rows)

    word_clause = Clause(
        clause_id="word-1",
        doc_type="word",
        number="1",
        text=_table_text(word_table),
        tables=[word_table],
    )
    pdf_clause = Clause(
        clause_id="pdf-1",
        doc_type="pdf",
        number="1",
        text=_table_text(pdf_table),
        tables=[pdf_table],
    )

    report = build_report(
        alignments=[Alignment(
            word_clause_id="word-1",
            pdf_clause_id="pdf-1",
            match_type="number",
            similarity=1.0,
        )],
        word_by={"word-1": word_clause},
        pdf_by={"pdf-1": pdf_clause},
        embed=_AlmostIdenticalEmbedding(),
        page_metas=[],
        thresholds={"identical": 0.98, "modified": 0.85},
        source="source.docx",
        target="target.pdf",
    )

    assert report.overall_risk == "medium"
    assert len(report.diffs) == 1
    assert report.diffs[0].status == "modified"
    assert any("表格" in reason for reason in report.diffs[0].risk_reasons)
    assert any(
        segment.op == "delete" and "产品17" in segment.text
        for segment in report.diffs[0].segments
    )


def test_text_missing_from_docx_table_is_described_as_pdf_addition():
    word_table = TableStructure(
        headers=["序号", "名称"], rows=[["1", ""]]
    )
    pdf_table = TableStructure(
        headers=["序号", "名称"], rows=[["1", "PDF独有文本"]]
    )

    reason = describe_table_change([word_table], [pdf_table])

    assert "新增文本" in reason
    assert "名称" in reason


def test_unmatched_adjacent_fragment_already_covered_by_paired_pdf_is_suppressed():
    """Word 多切出的相邻小块已在 PDF 配对条款中时，不再报 deleted。"""
    word_main = Clause(
        clause_id="word-1", doc_type="word", text="甲方应当按期付款。"
    )
    word_fragment = Clause(
        clause_id="word-2", doc_type="word", text="付款日期为2026年7月31日。"
    )
    pdf_merged = Clause(
        clause_id="pdf-1",
        doc_type="pdf",
        text="甲方应当按期付款。\n付款日期为2026年7月31日。",
    )

    report = build_report(
        alignments=[
            Alignment(
                word_clause_id="word-1",
                pdf_clause_id="pdf-1",
                match_type="semantic",
                similarity=0.9,
            ),
            Alignment(
                word_clause_id="word-2",
                pdf_clause_id=None,
                match_type="unmatched",
                similarity=0.0,
            ),
        ],
        word_by={"word-1": word_main, "word-2": word_fragment},
        pdf_by={"pdf-1": pdf_merged},
        embed=_AlmostIdenticalEmbedding(),
        page_metas=[],
        thresholds={"identical": 0.98, "modified": 0.85},
        source="source.docx",
        target="target.pdf",
    )

    assert report.unmatched_clauses == []
    assert report.summary["status_counts"].get("deleted", 0) == 0


def test_real_unmatched_clause_keeps_its_actual_text_in_report():
    """真实缺失的条款不得隐藏，报告必须展示 Word 原文。"""
    missing_text = "乙方应在收货后三日内完成验收。"
    missing = Clause(clause_id="word-1", doc_type="word", text=missing_text)

    report = build_report(
        alignments=[Alignment(
            word_clause_id="word-1",
            pdf_clause_id=None,
            match_type="unmatched",
            similarity=0.0,
        )],
        word_by={"word-1": missing},
        pdf_by={},
        embed=_AlmostIdenticalEmbedding(),
        page_metas=[],
        thresholds={"identical": 0.98, "modified": 0.85},
        source="source.docx",
        target="target.pdf",
    )

    assert len(report.unmatched_clauses) == 1
    assert any(
        segment.op == "delete" and segment.text == missing_text
        for segment in report.unmatched_clauses[0].segments
    )
