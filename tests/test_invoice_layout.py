"""金额统计 — 全电发票坐标版面解析器单元测试。

用伪造的 page 对象(模拟 pymupdf page.get_text("dict") 结构 + 真实发票坐标)
测试 invoice_layout 的确定性解析逻辑,不依赖真实 PDF 文件,不发起任何网络/LLM 调用。
"""
import pytest

from document_comparison.statement.amount_column import summarize_table
from document_comparison.statement.invoice_layout import (
    _normalize_money_cell,
    looks_like_invoice,
    parse_invoice_page,
)


class _FakePage:
    """模拟 pymupdf page,只实现 get_text("dict") 返回构造好的 span 坐标。

    spans: list[dict],每个含 text/bbox。用真实全电发票的坐标,保证算法贴近生产。
    """

    def __init__(self, spans):
        self._spans = spans

    def get_text(self, mode="text"):
        # 构造 pymupdf dict 结构
        lines = []
        for s in self._spans:
            lines.append({
                "bbox": s["bbox"],
                "spans": [{"text": s["text"], "bbox": s["bbox"]}],
            })
        return {"blocks": [{"lines": lines}]}


def _span(text, x0, y0, x1, y1):
    return {"text": text, "bbox": [x0, y0, x1, y1]}


# 真实全电发票坐标(来自实测 dump),3 个商品 + 合计行 + 价税合计
_INVOICE_SPANS = [
    # 页眉
    _span("电⼦发票（普通发票）", 194.5, 20.8, 394.5, 32),
    _span("发票号码：", 440.5, 35.3, 485.5, 45),
    _span("26427000000241649137", 486.5, 34.6, 569.7, 45),
    # 购买/销售方(简化)
    _span("名称：", 34.5, 102.7, 61.5, 112),
    _span("陈昊立", 59.5, 100.6, 86.5, 112),
    # 明细表头行 y≈153.3
    _span("项目名称", 46.5, 153.3, 82.5, 165),
    _span("规格型号", 120.5, 153.3, 156.5, 165),
    _span("单  位", 191.5, 153.3, 218.5, 165),
    _span("数  量", 265.5, 153.3, 292.5, 165),
    _span("单  价", 335.5, 153.3, 362.5, 165),
    _span("金  额", 408.5, 153.3, 435.5, 165),
    _span("税率/征收率", 447.5, 153.3, 497.0, 165),
    _span("税  额", 555.5, 153.3, 582.5, 165),
    # 商品1(名称跨两行)
    _span("*果类加工品*（ZC）沃隆", 16.0, 164.1, 116.3, 176),
    _span("750g", 122.0, 164.1, 138.5, 176),
    _span("盒", 200.5, 164.1, 209.5, 176),
    _span("1", 287.8, 164.1, 292.0, 176),
    _span("96.46", 343.2, 164.1, 362.0, 176),
    _span("96.46", 415.2, 164.1, 434.0, 176),
    _span("13%", 467.3, 164.1, 482.7, 176),
    _span("12.54", 563.7, 164.1, 582.5, 176),
    _span("每日纯坚果750g", 16.0, 177.1, 77.5, 189),  # 商品1名称续行
    # 商品2
    _span("*水果*和民猕猴桃礼盒L3", 16.0, 190.1, 113.5, 202),
    _span("详见包装", 122.0, 190.1, 158.0, 202),
    _span("盒", 200.5, 190.1, 209.5, 202),
    _span("1", 287.8, 190.1, 292.0, 202),
    _span("89.91", 343.2, 190.1, 362.0, 202),
    _span("89.91", 415.2, 190.1, 434.0, 202),
    _span("9%", 469.3, 190.1, 480.7, 202),
    _span("8.09", 567.9, 190.1, 582.5, 202),
    # 商品3(名称跨两行)
    _span("*焙烤食品*格力高百醇新", 16.0, 203.1, 113.6, 215),
    _span("385g", 122.0, 203.1, 138.5, 215),
    _span("袋", 200.5, 203.1, 209.5, 215),
    _span("1", 287.8, 203.1, 292.0, 215),
    _span("35.31", 343.2, 203.1, 362.0, 215),
    _span("35.31", 415.2, 203.1, 434.0, 215),
    _span("13%", 467.3, 203.1, 482.7, 215),
    _span("4.59", 567.9, 203.1, 582.5, 215),
    _span("年欢乐礼盒 385g", 16.0, 216.1, 79.3, 228),  # 商品3名称续行
    # 合计行
    _span("合", 60.5, 267.8, 69.5, 279),
    _span("计", 105.5, 267.8, 114.5, 279),
    _span("¥221.68", 402.1, 266.1, 434.0, 278),
    _span("¥25.22", 554.7, 266.1, 582.5, 278),
    # 价税合计
    _span("价税合计（大写）", 50.5, 285.8, 122.5, 297),
    _span("贰佰肆拾陆圆玖角整", 182.0, 284.1, 263.0, 296),
    _span("（小写）", 409.5, 285.8, 445.5, 297),
    _span("¥ 246.90", 446.0, 282.3, 487.3, 294),
    # 备注/开票人
    _span("100201-0276-2120362", 34.5, 302.1, 111.9, 314),
    _span("开票人：", 57.5, 372.8, 93.5, 384),
    _span("admin", 93.5, 371.6, 116.1, 383),
]


# —— looks_like_invoice ——

def test_looks_like_invoice_true():
    """全电发票特征命中。"""
    text = "电⼦发票（普通发票）\n...价税合计（大写）..."
    assert looks_like_invoice(text) is True


def test_looks_like_invoice_vat_keyword():
    """增值税发票也命中。"""
    text = "增值税电子普通发票\n价税合计 ¥100"
    assert looks_like_invoice(text) is True


def test_looks_like_invoice_false_for_statement():
    """对帐单不含发票特征 → 不走发票解析器。"""
    text = "对帐单\n已付 未付 金额\n合计"
    assert looks_like_invoice(text) is False


def test_looks_like_invoice_false_empty():
    assert looks_like_invoice("") is False


# —— parse_invoice_page ——

def test_parse_invoice_page_headers():
    """解析器识别出标准 8 列表头。"""
    page = _FakePage(_INVOICE_SPANS)
    table, _ = parse_invoice_page(page)
    assert table is not None
    assert table.headers == [
        "项目名称", "规格型号", "单位", "数量", "单价", "金额", "税率/征收率", "税额"
    ]


def test_parse_invoice_page_data_rows_count():
    """3 个商品 → 3 行数据(跨行名称续行已合并)。"""
    page = _FakePage(_INVOICE_SPANS)
    table, _ = parse_invoice_page(page)
    assert table is not None
    assert len(table.rows) == 3


def test_parse_invoice_page_amounts_correct():
    """金额列与税额列正确归位(单价 96.46 不被误当金额)。"""
    page = _FakePage(_INVOICE_SPANS)
    table, _ = parse_invoice_page(page)
    headers = table.headers
    amt_idx = headers.index("金额")
    tax_idx = headers.index("税额")
    unit_idx = headers.index("单价")
    amounts = [r[amt_idx] for r in table.rows]
    taxes = [r[tax_idx] for r in table.rows]
    unit_prices = [r[unit_idx] for r in table.rows]
    assert amounts == ["96.46元", "89.91元", "35.31元"]
    assert taxes == ["12.54元", "8.09元", "4.59元"]
    # 单价与金额值相同但分属不同列(靠 x 坐标区分)
    assert unit_prices == ["96.46元", "89.91元", "35.31元"]


def test_parse_invoice_page_continuation_merged():
    """跨行商品名合并:沃隆+每日纯坚果750g → 同一行的项目名称。"""
    page = _FakePage(_INVOICE_SPANS)
    table, _ = parse_invoice_page(page)
    headers = table.headers
    name_idx = headers.index("项目名称")
    assert table.rows[0][name_idx] == "*果类加工品*（ZC）沃隆每日纯坚果750g"
    assert table.rows[2][name_idx] == "*焙烤食品*格力高百醇新年欢乐礼盒 385g"


def test_parse_invoice_page_grand_total():
    """价税合计小写金额正确提取。"""
    page = _FakePage(_INVOICE_SPANS)
    _, grand_total = parse_invoice_page(page)
    assert grand_total == 246.90


def test_parse_invoice_page_excludes_total_rows():
    """合计行(¥221.68/¥25.22)不进入数据行(避免与明细重复计入)。"""
    page = _FakePage(_INVOICE_SPANS)
    table, _ = parse_invoice_page(page)
    all_cells = [cell for row in table.rows for cell in row]
    assert not any("221.68" in c for c in all_cells)
    assert not any("25.22" in c for c in all_cells)


def test_parse_invoice_page_returns_none_for_non_invoice():
    """非发票页面(无表头行)返回 None。"""
    spans = [_span("对帐单", 50, 50, 100, 60), _span("已付 100元", 50, 80, 120, 90)]
    page = _FakePage(spans)
    table, grand_total = parse_invoice_page(page)
    assert table is None
    assert grand_total is None


def test_parse_invoice_page_empty():
    """空页面返回 None。"""
    page = _FakePage([])
    table, grand_total = parse_invoice_page(page)
    assert table is None
    assert grand_total is None


# —— _normalize_money_cell ——

def test_normalize_money_cell_pure_number():
    """纯数字补「元」单位(发票明细金额无单位)。"""
    assert _normalize_money_cell("96.46") == "96.46元"
    assert _normalize_money_cell("1680") == "1680元"
    assert _normalize_money_cell("1,680.00") == "1,680.00元"


def test_normalize_money_cell_keeps_unit():
    """带 ¥/元 的原样保留。"""
    assert _normalize_money_cell("¥96.46") == "¥96.46"
    assert _normalize_money_cell("96.46元") == "96.46元"


def test_normalize_money_cell_text_unchanged():
    """非纯数字文本不变(如商品名)。"""
    assert _normalize_money_cell("详见包装") == "详见包装"
    assert _normalize_money_cell("") == ""


@pytest.mark.parametrize("raw", ["-127913.87", "−127913.87", "－127913.87", "- 127913.87"])
def test_normalize_negative_money_cell(raw):
    assert _normalize_money_cell(raw) == "-127913.87元"


def test_invoice_discount_amount_and_tax_are_deducted():
    # 使用反馈截图的六行金额；坐标为合成数据，不依赖真实发票文件。
    amounts = ["74690.27", "50442.47", "673685.92", "55752.21", "819960.18", "-127913.87"]
    taxes = ["9709.73", "6557.53", "87579.18", "7247.79", "106594.82", "-16628.80"]
    spans = [
        _positioned_span("项目名称", 10, 100),
        _positioned_span("金额", 210, 100),
        _positioned_span("税额", 310, 100),
    ]
    for i, (amount, tax) in enumerate(zip(amounts, taxes)):
        y = 120 + i * 20
        spans.extend([
            _positioned_span(f"商品{i}", 10, y),
            _positioned_span(amount, 210, y),
            _positioned_span(tax, 310, y),
        ])
    spans.extend([
        _positioned_span("合计", 10, 250),
        _positioned_span("¥1546617.18", 210, 250),
        _positioned_span("¥201060.25", 310, 250),
        _positioned_span("价税合计（大写）", 10, 270),
        _positioned_span("¥1747677.43", 310, 270),
    ])
    table, total = parse_invoice_page(_FakePage(spans))
    summary = summarize_table(table, file_index=0, file_name="synthetic.pdf", table_index=0, page_index=0)
    assert len(table.rows) == 6
    assert len(summary.items) == 12
    assert [item.value for item in summary.items if item.value < 0] == [-127913.87, -16628.80]
    assert summary.column_sums == {"金额": 1546617.18, "税额": 201060.25}
    assert summary.tax_inclusive_total == total == 1747677.43


# 合成票据:货币符号与数字分片时不重复计入合计行。
def _positioned_span(text, x, y, width=30):
    return {"text": text, "bbox": (x, y, x + width, y + 10)}


@pytest.mark.parametrize("split", [False, True])
@pytest.mark.parametrize("offset", [-0.3, 0.3])
def test_total_row_is_excluded_with_split_currency_spans(split, offset):
    spans = [
        _positioned_span("项目名称", 10, 100), _positioned_span("金额", 210, 100),
        _positioned_span("税额", 310, 100),
        _positioned_span("商品A", 10, 120), _positioned_span("100.00", 210, 120),
        _positioned_span("13.00", 310, 120),
        _positioned_span("商品B", 10, 140), _positioned_span("200.00", 210, 140),
        _positioned_span("26.00", 310, 140),
        _positioned_span("合", 10, 183), _positioned_span("计", 50, 183),
        _positioned_span("价税合计（大写）", 10, 203),
        _positioned_span("（小写）", 250, 203),
    ]
    for amount, x, y in [("300.00", 205, 180), ("39.00", 305, 180), ("339.00", 305, 200)]:
        if split:
            spans.extend([_positioned_span("¥", x, y, 5), _positioned_span(amount, x + 5, y + offset)])
        else:
            spans.append(_positioned_span("¥" + amount, x, y, 35))
    table, grand_total = parse_invoice_page(_FakePage(spans))
    assert table is not None
    assert len(table.rows) == 2
    summary = summarize_table(table, file_index=0, file_name="invoice.pdf", table_index=0, page_index=0)
    assert summary.column_sums == {"金额": 300.0, "税额": 39.0}
    assert summary.tax_inclusive_total == 339.0
    assert grand_total == 339.0
