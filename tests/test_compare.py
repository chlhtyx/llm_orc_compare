"""比对与风险分级测试。"""
from document_comparison.compare.adjudication import adjudicate_clause_pair
from document_comparison.compare.diff import char_diff, describe_table_change
from document_comparison.compare.elements import (
    canonicalize_contract_text,
    elements_changed,
    extract_key_elements,
    reviewable_formatting_change,
)
from document_comparison.compare.risk import classify_diff, max_risk
from document_comparison.models import (
    Alignment,
    Clause,
    KeyElement,
    TableStructure,
    TamperReport,
)
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


def test_high_similarity_text_change_is_never_identical():
    """零容忍策略下，语义再相近也不能抹掉已确认字符变化。"""
    status, risk, _ = classify_diff(
        word_text="甲方承担全部责任",
        pdf_text="乙方承担全部责任",
        similarity=0.9999,
        key_elements=[],
        sim_identical=0.98,
        sim_modified=0.85,
    )
    assert status == "modified"
    assert risk == "low"


def test_canonical_contract_facts_remove_representation_only_differences():
    """金额千分位和日期书写形式不同，但 canonical value 相同。"""
    word = "合同金额100000元，交付日为2026-07-01。"
    pdf = "合同金额100,000.00元，交付日为2026年7月1日。"

    assert canonicalize_contract_text(word) == canonicalize_contract_text(pdf)
    changed = elements_changed(extract_key_elements(word), extract_key_elements(pdf))
    assert not any(element.changed for element in changed)


def test_canonical_contract_text_removes_safe_cjk_spacing_and_quote_styles():
    """中文 OCR 字间空格、标点旁空格和弯直引号只是安全排版差异。"""
    word = '甲方：“按期交付货物”。'
    pdf = '甲 方 : "按期交付货物"。'

    assert canonicalize_contract_text(word) == canonicalize_contract_text(pdf)


def test_canonical_contract_text_preserves_english_word_boundaries():
    """英文单词边界可能改变含义，不能沿用“删除全部空格”的旧规则。"""
    assert canonicalize_contract_text("force majeure") != canonicalize_contract_text(
        "forcemajeure"
    )
    assert reviewable_formatting_change("force majeure", "forcemajeure") == "spacing"


def test_canonical_amount_accepts_ocr_grouping_spaces():
    assert canonicalize_contract_text("金额100 000.00元") == canonicalize_contract_text(
        "金额100000元"
    )


def test_canonical_identifier_accepts_ocr_character_spacing():
    assert canonicalize_contract_text(
        "合同编号: Q G S C 2 6 0 7 0 8 0 0 1 0"
    ) == canonicalize_contract_text("合同编号:QGSC2607080010")


def test_account_extraction_tolerates_ocr_cjk_spacing():
    """OCR 在多字关键词中间插空格(账 号)不能让账号要素失配而误报高风险变更。

    回归:此前 PDF 侧因 "账 号" 抽不到 account,与 Word 侧集合不同,
    被判 "高风险要素变更:account, account"。
    """
    word = "XX市恒信商贸有限公司账号:6222081001008899776"
    pdf = "XX市恒信商贸有限公司账 号:6222081001008899776"

    assert extract_key_elements(pdf).get("account") == ["6222081001008899776"]
    assert canonicalize_contract_text(word) == canonicalize_contract_text(pdf)
    changed = elements_changed(extract_key_elements(word), extract_key_elements(pdf))
    assert not any(e.kind == "account" and e.changed for e in changed)


def test_reviewable_punctuation_does_not_include_structural_numeric_marks():
    assert reviewable_formatting_change("本条有效。", "本条有效") == "punctuation"
    assert reviewable_formatting_change("第1.1条", "第11条") is None
    assert reviewable_formatting_change("违约金为-5%", "违约金为5%") is None


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


class _TextAwareEmbedding:
    def embed_batch(self, texts):
        return texts

    def similarity(self, left, right):
        return 1.0 if left == right else 0.5


def test_safe_spacing_and_quote_styles_do_not_enter_diff_report():
    decision = adjudicate_clause_pair(
        Clause(clause_id="w", doc_type="word", text='甲方：“按期交付”。'),
        Clause(clause_id="p", doc_type="pdf", text='甲 方 : "按期交付"。'),
        similarity=0.99,
        sim_identical=0.98,
        sim_modified=0.85,
    )

    assert decision.status == "identical"
    assert decision.verdict == "clean"


def test_missing_sentence_punctuation_is_review_not_clean_or_confirmed_change():
    decision = adjudicate_clause_pair(
        Clause(clause_id="w", doc_type="word", text="甲方应按期交付。"),
        Clause(clause_id="p", doc_type="pdf", text="甲方应按期交付"),
        similarity=0.99,
        sim_identical=0.98,
        sim_modified=0.85,
    )

    assert decision.status == "modified"
    assert decision.verdict == "needs_review"
    assert decision.confidence == "low"
    assert any("标点" in reason for reason in decision.reasons)


def test_unsafe_english_spacing_is_review_not_clean():
    decision = adjudicate_clause_pair(
        Clause(clause_id="w", doc_type="word", text="force majeure"),
        Clause(clause_id="p", doc_type="pdf", text="forcemajeure"),
        similarity=0.99,
        sim_identical=0.98,
        sim_modified=0.85,
    )

    assert decision.status == "modified"
    assert decision.verdict == "needs_review"
    assert any("空格" in reason for reason in decision.reasons)


def test_structural_numeric_punctuation_remains_confirmed_change():
    decision = adjudicate_clause_pair(
        Clause(clause_id="w", doc_type="word", text="第1.1条"),
        Clause(clause_id="p", doc_type="pdf", text="第11条"),
        similarity=0.99,
        sim_identical=0.98,
        sim_modified=0.85,
    )

    assert decision.status == "modified"
    assert decision.verdict == "changed"


def test_table_spacing_only_change_is_review_not_confirmed_change():
    decision = adjudicate_clause_pair(
        Clause(
            clause_id="w",
            doc_type="word",
            text="付款信息",
            tables=[TableStructure(headers=["条款"], rows=[["force majeure"]])],
        ),
        Clause(
            clause_id="p",
            doc_type="pdf",
            text="付款信息",
            tables=[TableStructure(headers=["条款"], rows=[["forcemajeure"]])],
        ),
        similarity=0.99,
        sim_identical=0.98,
        sim_modified=0.85,
    )

    assert decision.status == "modified"
    assert decision.verdict == "needs_review"
    assert any("空格" in reason for reason in decision.reasons)


def test_field_anchor_recomputes_company_name_similarity():
    """field=甲方只负责配对；公司名称正文变化仍必须进入差异报告。"""
    word_clause = Clause(
        clause_id="word-1",
        doc_type="word",
        field_key="甲方",
        text="武汉高外股份有限公司",
    )
    pdf_clause = Clause(
        clause_id="pdf-1",
        doc_type="pdf",
        field_key="甲方",
        text="武汉高德红外股份有限公司",
    )

    report = build_report(
        alignments=[Alignment(
            word_clause_id="word-1",
            pdf_clause_id="pdf-1",
            match_type="field",
            similarity=1.0,
        )],
        word_by={"word-1": word_clause},
        pdf_by={"pdf-1": pdf_clause},
        embed=_TextAwareEmbedding(),
        page_metas=[],
        thresholds={"identical": 0.98, "modified": 0.85},
        source="source.docx",
        target="target.pdf",
    )

    assert len(report.diffs) == 1
    assert report.diffs[0].status == "modified"
    assert any(segment.op == "insert" and "德红" in segment.text
               for segment in report.diffs[0].segments)


def test_long_clause_actor_change_survives_near_identical_similarity():
    """长条款中的甲乙方互换不能被整条余弦相似度稀释。"""
    prefix = "双方应遵循诚实信用原则并按合同约定履行各项义务。" * 12
    word_clause = Clause(
        clause_id="word-1",
        doc_type="word",
        number="1",
        text=prefix + "如发生违约，甲方承担全部违约责任。",
    )
    pdf_clause = Clause(
        clause_id="pdf-1",
        doc_type="pdf",
        number="1",
        text=prefix + "如发生违约，乙方承担全部违约责任。",
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

    assert report.change_status == "changed"
    assert len(report.diffs) == 1
    assert report.diffs[0].verdict == "changed"
    assert report.diffs[0].risk_level == "high"
    assert any(element.kind == "party" for element in report.key_elements)


def test_ocr_confusable_critical_value_is_review_not_clean():
    """0→O 可能是 OCR 噪声，但在图像复核前绝不能判 clean。"""
    word_clause = Clause(
        clause_id="word-1", doc_type="word", number="1", text="合同金额为100万元。"
    )
    pdf_clause = Clause(
        clause_id="pdf-1", doc_type="pdf", number="1", text="合同金额为1OO万元。"
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
        embed=_TextAwareEmbedding(),
        page_metas=[],
        thresholds={"identical": 0.98, "modified": 0.85},
        source="source.docx",
        target="target.pdf",
    )

    assert report.change_status == "needs_review"
    assert report.diffs[0].verdict == "needs_review"
    assert report.diffs[0].confidence == "low"
    assert report.diffs[0].risk_level == "high"


def test_llm_judge_cannot_downgrade_confirmed_change(monkeypatch):
    """LLM 只能提供建议，不能把确定性金额变化降为 none。"""
    monkeypatch.setattr(
        "document_comparison.report.builder.llm_judge_diff",
        lambda *_args, **_kwargs: ("none", ["LLM 认为可能是 OCR 噪声"]),
    )
    word_clause = Clause(
        clause_id="word-1", doc_type="word", number="1", text="金额100万元"
    )
    pdf_clause = Clause(
        clause_id="pdf-1", doc_type="pdf", number="1", text="金额200万元"
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
        embed=_TextAwareEmbedding(),
        page_metas=[],
        thresholds={"identical": 0.98, "modified": 0.85},
        source="source.docx",
        target="target.pdf",
        enable_llm_judge=True,
    )

    assert report.change_status == "changed"
    assert report.overall_risk == "high"
    assert report.diffs[0].risk_level == "high"


def test_legacy_report_infers_change_status_from_existing_diffs():
    """升级前保存的 JSON 没有新字段，重新加载时不能错误显示 clean。"""
    report = TamperReport.model_validate({
        "source": "source.docx",
        "target": "target.pdf",
        "overall_risk": "medium",
        "diffs": [{
            "alignment_id": "al1",
            "status": "modified",
            "risk_level": "medium",
        }],
    })

    assert report.change_status == "changed"
    assert report.diffs[0].verdict == "changed"
    assert report.diffs[0].confidence == "high"


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
