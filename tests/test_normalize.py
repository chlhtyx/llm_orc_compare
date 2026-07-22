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


def test_normalize_underset_emphasis_dot():
    # OCR 公式识别把中文「着重号」(字下方圆点)误判为数学下标 \underset{\cdot}{乙}
    s = r"\(\underset{\cdot}{乙}\)方(签字/盖章):XX市恒信商贸有限公司"
    assert normalize_text(s) == "乙方(签字/盖章):XX市恒信商贸有限公司"


def test_normalize_underset_plain_dot():
    # 下标为裸点 . 的变体同样还原为主字符
    assert normalize_text(r"\(\underset{.}{X}\)") == "X"


def test_normalize_underset_bullet():
    # 下标为 Unicode 项目符号 • 的变体
    assert normalize_text(r"\underset{•}{甲}方") == "甲方"


def test_normalize_underset_general():
    # 通用 \underset{下标}{主字符} 保留主字符,丢弃下标
    assert normalize_text(r"\underset{\mathrm{注}}{条}款") == "条款"


def test_normalize_latex_delimiters():
    # 残余 LaTeX 行内/行间定界符直接剥除
    assert normalize_text(r"金额\(100\)元") == "金额100元"
    assert normalize_text("和$$差$$计") == "和差计"


def test_normalize_latex_idempotent():
    s = r"\(\underset{\cdot}{乙}\)方(签字/盖章):XX市恒信商贸有限公司"
    once = normalize_text(s)
    assert normalize_text(once) == once


def test_normalize_no_latex_passthrough():
    # 不含 LaTeX 的文本走快速路径,行为与之前一致
    assert normalize_text("正常文本无公式") == "正常文本无公式"
