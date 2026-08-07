"""对齐层测试。"""
from document_comparison.align import align_clauses
from document_comparison.embed.mock import MockEmbedding
from document_comparison.models import Clause


def _clause(cid, number, text, doc_type="word", field_key=""):
    return Clause(
        clause_id=cid, doc_type=doc_type, number=number, text=text, field_key=field_key
    )


def test_align_by_number():
    word = [_clause("w1", "1", "甲方义务内容"), _clause("w2", "2", "金额一百万")]
    pdf = [_clause("p1", "1", "甲方义务内容", "pdf"), _clause("p2", "2", "金额一百万", "pdf")]
    al = align_clauses(word, pdf, MockEmbedding(), threshold=0.85)
    by_number = [a for a in al if a.match_type == "number"]
    assert len(by_number) == 2


def test_align_semantic_fallback():
    # pdf 编号 OCR 错(2→缺失),正文精确一致时无需调用语义模型
    word = [_clause("w1", "1", "甲方应按时交付货物并保证质量")]
    pdf = [_clause("p1", "", "甲方应按时交付货物并保证质量", "pdf")]
    al = align_clauses(word, pdf, MockEmbedding(), threshold=0.5)
    exact = [a for a in al if a.match_type == "normalized_exact"]
    assert len(exact) == 1


def test_align_added_deleted():
    word = [_clause("w1", "1", "唯一条款")]
    pdf = [_clause("p1", "1", "唯一条款", "pdf"), _clause("p2", "2", "多出来的条款", "pdf")]
    al = align_clauses(word, pdf, MockEmbedding(), threshold=0.85)
    added = [a for a in al if a.word_clause_id is None and a.pdf_clause_id]
    assert len(added) == 1


def test_align_by_field_key():
    """无编号键值块(甲方/乙方/地址)按 field_key 配对。"""
    word = [
        _clause("w1", "", "XX公司", field_key="甲方"),
        _clause("w2", "", "上海市", field_key="地址"),
        _clause("w3", "", "YY公司", field_key="乙方"),
    ]
    pdf = [
        _clause("p1", "", "XX公司", "pdf", field_key="甲方"),
        _clause("p2", "", "上海市", "pdf", field_key="地址"),
        _clause("p3", "", "YY公司", "pdf", field_key="乙方"),
    ]
    al = align_clauses(word, pdf, MockEmbedding(), threshold=0.85)
    by_field = [a for a in al if a.match_type == "field"]
    assert len(by_field) == 3
    # 甲方配甲方、地址配地址
    pairs = {(a.word_clause_id, a.pdf_clause_id) for a in by_field}
    assert ("w1", "p1") in pairs
    assert ("w2", "p2") in pairs
    assert ("w3", "p3") in pairs


def test_align_field_key_same_role_multiple():
    """同 field_key 多条(甲方首部 + 甲方签字页)按出现顺序配对。"""
    word = [
        _clause("w1", "", "首部甲方信息", field_key="甲方"),
        _clause("w2", "", "签字页甲方", field_key="甲方"),
    ]
    pdf = [
        _clause("p1", "", "首部甲方信息", "pdf", field_key="甲方"),
        _clause("p2", "", "签字页甲方", "pdf", field_key="甲方"),
    ]
    al = align_clauses(word, pdf, MockEmbedding(), threshold=0.85)
    by_field = [a for a in al if a.match_type == "field"]
    assert len(by_field) == 2
    # 按顺序:w1-p1, w2-p2
    assert by_field[0].word_clause_id == "w1"
    assert by_field[0].pdf_clause_id == "p1"
    assert by_field[1].word_clause_id == "w2"
    assert by_field[1].pdf_clause_id == "p2"


def test_repeated_number_missing_on_one_side_does_not_shift_anchor_pairs():
    """重复子编号缺失一条时，后续同编号不能按出现顺序被强制错配。"""
    class TextEmbedding:
        def embed_batch(self, texts):
            return texts

        def similarity(self, left, right):
            return 0.95 if left == right else 0.10

    word = [
        _clause("w1", "1", "第一章交付要求"),
        _clause("w2", "1", "第二章付款要求"),
    ]
    pdf = [
        _clause("p1", "1", "第二章付款要求", "pdf"),
    ]

    alignments = align_clauses(word, pdf, TextEmbedding(), threshold=0.85)

    assert any(
        item.word_clause_id == "w2" and item.pdf_clause_id == "p1"
        for item in alignments
    )
    assert any(
        item.word_clause_id == "w1"
        and item.pdf_clause_id is None
        and item.match_type == "unmatched"
        for item in alignments
    )


def test_semantic_alignment_cannot_cross_confirmed_anchor_boundaries():
    """锚点前后的剩余条款必须分段匹配，不能跨越已确认编号锚点。"""
    class CrossingEmbedding:
        def embed_batch(self, texts):
            return texts

        def similarity(self, left, right):
            return 0.95 if {left, right} == {"锚点前条款", "锚点后新增文本"} else 0.1

    word = [
        _clause("w-anchor-1", "1", "第一条"),
        _clause("w-before", "", "锚点前条款"),
        _clause("w-anchor-2", "2", "第二条"),
    ]
    pdf = [
        _clause("p-anchor-1", "1", "第一条", "pdf"),
        _clause("p-anchor-2", "2", "第二条", "pdf"),
        _clause("p-after", "", "锚点后新增文本", "pdf"),
    ]

    alignments = align_clauses(word, pdf, CrossingEmbedding(), threshold=0.85)

    assert not any(
        item.word_clause_id == "w-before" and item.pdf_clause_id == "p-after"
        for item in alignments
    )
    assert any(
        item.word_clause_id == "w-before" and item.pdf_clause_id is None
        for item in alignments
    )
    assert any(
        item.word_clause_id is None and item.pdf_clause_id == "p-after"
        for item in alignments
    )


def test_aligns_layout_whitespace_variants_without_embedding():
    """OCR 字间空格只是版式噪声，不应生成一条 added 和一条 deleted。"""
    class RejectingEmbedding:
        def embed_batch(self, texts):
            return texts

        def similarity(self, _left, _right):
            return 0.0

    word = [_clause(
        "w1",
        "",
        "采购合同(简易版)\n申购单编号:QGSC2607080010合同编号:SSC2607130114",
    )]
    pdf = [_clause(
        "p1",
        "",
        "采 购 合 同(简易版)\n申购单编号: QGSC2607080010 合同编号: SSC2607130114",
        "pdf",
    )]

    alignments = align_clauses(word, pdf, RejectingEmbedding(), threshold=0.85)

    assert len(alignments) == 1
    assert alignments[0].word_clause_id == "w1"
    assert alignments[0].pdf_clause_id == "p1"
    assert alignments[0].match_type == "normalized_exact"


def test_normalized_exact_does_not_hide_identifier_change():
    """去空白匹配仍须保留字符差异，合同编号变化不能被配对为完全一致。"""
    class RejectingEmbedding:
        def embed_batch(self, texts):
            return texts

        def similarity(self, _left, _right):
            return 0.0

    word = [_clause("w1", "", "合同编号: SSC2607130114")]
    pdf = [_clause("p1", "", "合同编号: SSC2607130115", "pdf")]

    alignments = align_clauses(word, pdf, RejectingEmbedding(), threshold=0.85)

    assert all(a.match_type != "normalized_exact" for a in alignments)
    assert len([a for a in alignments if a.match_type == "unmatched"]) == 2


def test_unmatched_reason_identifies_threshold_and_text_changes():
    """相近标题未达阈值时，应说明阈值和主要字符差异，而非只报 added/deleted。"""
    class BelowThresholdEmbedding:
        def embed_batch(self, texts):
            return texts

        def similarity(self, _left, _right):
            return 0.84

    word = [_clause(
        "w1",
        "",
        "采 购 合 同(简易版)\n"
        "申购单编号: QGSC260708010 合同编号: SSC2607130114 合同必读",
    )]
    pdf = [_clause(
        "p1",
        "",
        "采购合同(简易版)\n"
        "申购单编号:QGSC2607080010合同编号:SSC2607130114",
        "pdf",
    )]

    alignments = align_clauses(word, pdf, BelowThresholdEmbedding(), threshold=0.85)

    assert len(alignments) == 2
    for alignment in alignments:
        assert "0.840" in alignment.alignment_reason
        assert "0.850" in alignment.alignment_reason
        assert "原始合同独有“合同必读”" in alignment.alignment_reason
        assert "回收件独有“0”" in alignment.alignment_reason


def test_semantic_alignment_embeds_both_sides_in_one_batch():
    class CountingEmbedding(MockEmbedding):
        def __init__(self):
            self.calls = []

        def embed_batch(self, texts):
            self.calls.append(list(texts))
            return [self.embed(text) for text in texts]

    embed = CountingEmbedding()
    word = [_clause("w1", "", "甲方应按时交付货物")]
    pdf = [_clause("p1", "", "甲方应按期交付货物", "pdf")]
    align_clauses(word, pdf, embed, threshold=0.5)
    assert embed.calls == [["甲方应按时交付货物", "甲方应按期交付货物"]]


def test_semantic_alignment_uses_global_monotonic_optimum():
    """首个 PDF 的局部最佳会抢占后项时，应选择总分更高的单调配对。"""
    class MatrixEmbedding:
        matrix = {
            ("p1", "w1"): 0.89,
            ("p1", "w2"): 0.90,
            ("p2", "w1"): 0.10,
            ("p2", "w2"): 0.88,
        }

        def embed_batch(self, texts):
            return texts

        def similarity(self, left, right):
            return self.matrix.get((left, right), self.matrix.get((right, left), 0.0))

    word = [_clause("w1", "", "w1"), _clause("w2", "", "w2")]
    pdf = [_clause("p1", "", "p1", "pdf"), _clause("p2", "", "p2", "pdf")]

    alignments = align_clauses(word, pdf, MatrixEmbedding(), threshold=0.85)
    semantic = [item for item in alignments if item.match_type == "semantic"]

    assert [(item.word_clause_id, item.pdf_clause_id) for item in semantic] == [
        ("w1", "p1"),
        ("w2", "p2"),
    ]
    assert not [item for item in alignments if item.match_type == "unmatched"]


def test_semantic_alignment_supports_one_word_to_two_pdf_clauses():
    """解析边界不同时，应原生表达 1↔2，而不是生成一条误报 added。"""
    class SplitAwareEmbedding:
        def embed_batch(self, texts):
            return texts

        def similarity(self, left, right):
            values = {left, right}
            if values == {"交付后付款\n验收合格", "交付后付款 验收合格"}:
                return 0.96
            return 0.20

    word = [_clause("w1", "", "交付后付款 验收合格")]
    pdf = [
        _clause("p1", "", "交付后付款", "pdf"),
        _clause("p2", "", "验收合格", "pdf"),
    ]

    alignments = align_clauses(word, pdf, SplitAwareEmbedding(), threshold=0.85)

    assert len(alignments) == 1
    assert alignments[0].word_clause_ids == ["w1"]
    assert alignments[0].pdf_clause_ids == ["p1", "p2"]
    assert alignments[0].match_type == "semantic"


def test_llm_resolver_can_select_bounded_near_threshold_split_candidate():
    """LLM 只能从代码候选中选择，并可确认略低于常规阈值的 1↔2 歧义项。"""
    class SplitEmbedding:
        def embed_batch(self, texts):
            return texts

        def similarity(self, left, right):
            if "\n" in left or "\n" in right:
                return 0.80
            return 0.30

    def resolver(candidates):
        split = next(
            item
            for item in candidates
            if item.word_clause_ids == ("w1",)
            and item.pdf_clause_ids == ("p1", "p2")
        )
        return [
            {
                "candidate_id": split.candidate_id,
                "confidence": 0.92,
                "reason": "同一付款条款被 PDF 解析为相邻两段",
            }
        ]

    alignments = align_clauses(
        [_clause("w1", "", "交付后付款并在验收后结清")],
        [
            _clause("p1", "", "交付后付款", "pdf"),
            _clause("p2", "", "验收后结清", "pdf"),
        ],
        SplitEmbedding(),
        threshold=0.85,
        llm_resolver=resolver,
    )

    assert len(alignments) == 1
    assert alignments[0].match_type == "llm"
    assert alignments[0].pdf_clause_ids == ["p1", "p2"]
    assert "0.920" in alignments[0].alignment_reason


def test_llm_resolver_can_break_close_one_to_one_candidate_tie():
    """第一、第二候选分数接近时，LLM 可在受限 1↔1 候选中重排。"""
    class TieEmbedding:
        def embed_batch(self, texts):
            return texts

        def similarity(self, left, right):
            if "\n" in left or "\n" in right:
                return 0.10
            return {
                ("候选甲", "待匹配条款"): 0.84,
                ("候选乙", "待匹配条款"): 0.82,
            }.get((left, right), 0.0)

    def resolver(candidates):
        selected = next(
            item for item in candidates if item.pdf_clause_ids == ("p1",)
        )
        return [{
            "candidate_id": selected.candidate_id,
            "confidence": 0.90,
            "reason": "主体和履约条件更一致",
        }]

    alignments = align_clauses(
        [_clause("w1", "", "待匹配条款")],
        [
            _clause("p1", "", "候选甲", "pdf"),
            _clause("p2", "", "候选乙", "pdf"),
        ],
        TieEmbedding(),
        threshold=0.85,
        llm_resolver=resolver,
    )

    selected = next(item for item in alignments if item.word_clause_id == "w1")
    assert selected.pdf_clause_id == "p1"
    assert selected.match_type == "llm"
