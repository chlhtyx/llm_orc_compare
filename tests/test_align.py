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
    # pdf 编号 OCR 错(2→缺失),靠语义匹配
    word = [_clause("w1", "1", "甲方应按时交付货物并保证质量")]
    pdf = [_clause("p1", "", "甲方应按时交付货物并保证质量", "pdf")]
    al = align_clauses(word, pdf, MockEmbedding(), threshold=0.5)
    sem = [a for a in al if a.match_type == "semantic"]
    assert len(sem) == 1


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


def test_semantic_alignment_embeds_both_sides_in_one_batch():
    class CountingEmbedding(MockEmbedding):
        def __init__(self):
            self.calls = []

        def embed_batch(self, texts):
            self.calls.append(list(texts))
            return [self.embed(text) for text in texts]

    embed = CountingEmbedding()
    word = [_clause("w1", "", "甲方应按时交付货物")]
    pdf = [_clause("p1", "", "甲方应按时交付货物", "pdf")]
    align_clauses(word, pdf, embed, threshold=0.5)
    assert embed.calls == [["甲方应按时交付货物", "甲方应按时交付货物"]]
