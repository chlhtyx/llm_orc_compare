"""对齐层测试。"""
from document_comparison.align import align_clauses
from document_comparison.embed.mock import MockEmbedding
from document_comparison.models import Clause


def _clause(cid, number, text, doc_type="word"):
    return Clause(clause_id=cid, doc_type=doc_type, number=number, text=text)


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
