"""文本归一化测试。"""
from document_comparison.structure.normalize import normalize_text


def test_normalize_fullwidth_digits():
    assert normalize_text("金额１２３元") == "金额123元"


def test_normalize_whitespace():
    # 全角空格 NFKC→半角,行内多空白合并为单空格
    assert normalize_text("  甲　方   乙方\n ") == "甲 方 乙方"


def test_normalize_preserves_newline():
    # 换行是结构边界,必须保留
    assert normalize_text("第一条\n第二条") == "第一条\n第二条"


def test_normalize_empty():
    assert normalize_text("") == ""


def test_normalize_nfkc_punct():
    # NFKC 把全角句号等统一(中文标点保留语义,此处仅验证幂等)
    s = normalize_text("第１条。")
    assert "第1条" in s
