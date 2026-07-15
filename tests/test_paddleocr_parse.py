"""PaddleOCR-VL 纯文本/Markdown 输出解析测试。

覆盖 PaddleOCR-VL-1.5 在 SiliconFlow 等平台上的实际输出格式:
- Markdown 表格(``|`` 分隔,含 ``---`` 分隔行)→ label=table + TableStructure
- 普通段落/字段行 → label=text
- 含 ``<|LOC_|>`` 标记的早期格式 → LOC 解析分支
- 配置:paddleocr 后端直接复用 llm_*(无独立 paddleocr_* 字段)
"""
from __future__ import annotations

from document_comparison.models import PageMeta
from document_comparison.ocr.paddleocr_http import (
    _parse_loc_content,
    _parse_plain_content,
    _plain_text_to_table,
)
from document_comparison.report.builder import _normalize_regions


def _meta() -> PageMeta:
    return PageMeta(
        page_index=0,
        width_px=1654, height_px=2339,
        pdf_width_pt=595, pdf_height_pt=842,
    )


# 来自 PaddleOCR-VL-1.5 实测输出(简短「OCR」指令,已截取表格区)
_PAGE0_PLAIN = """采购合同(简易版)

申购单编号：QCSC2607080010 合同编号：SSC2607130114

甲方（买受人）：武汉高德红外股份有限公司

乙方（出卖人）：武汉盈如鑫虹科技有限公司

1. 采购产品明细：

序号 | 物料编码 | 产品名称 | 不含税单价（元） | 含税单价（元） | 数量 | 单位 | 含税总价（元） | 生产厂家 | 交货期 | 备注
--- | --- | --- | --- | --- | --- | --- | --- | --- | --- | ---
1 | YA030000 | 1530A | 1.1062 | 1.25 | 200 | 米 | 250.00 | 凌云公司 | 2026-08-03 |
2 | YA030000 | 1531A | 4.2920 | 4.85 | 100 | 米 | 485.00 | 凌云公司 | 2026-08-03 |

含税总金额合计（RMB） | 人民币小写：2815元（人民币大写：贰仟捌佰壹拾伍元整）

其他说明：以上含税总金额为包含税费、运费等各项费用的到货价"""


def test_plain_parses_markdown_table():
    """Markdown 表格行应归为单个 label=table 的 Block,带 TableStructure。"""
    blocks = _parse_plain_content(_PAGE0_PLAIN, 0, _meta())
    tables = [b for b in blocks if b.label == "table"]
    assert len(tables) == 1
    tbl = tables[0].table
    assert tbl is not None
    # 表头 11 列
    assert len(tbl.headers) == 11
    assert tbl.headers[0] == "序号"
    # 数据行 2 行(明细)+ 2 行(合计/说明,因它们也含 |)
    assert len(tbl.rows) >= 2
    assert tbl.rows[0][0] == "1"
    # 不含税单价在第 3 列(序号0 物料编码1 产品名称2)
    assert tbl.rows[0][3] == "1.1062"


def test_plain_parses_paragraphs():
    """非表格行应归为 label=text 的 Block。"""
    blocks = _parse_plain_content(_PAGE0_PLAIN, 0, _meta())
    texts = [b for b in blocks if b.label == "text"]
    contents = [b.content for b in texts]
    assert any("采购合同" in c for c in contents)
    assert any("甲方" in c and "武汉高德红外" in c for c in contents)
    assert any("乙方" in c and "武汉盈如鑫虹" in c for c in contents)


def test_plain_strips_md_separator():
    """Markdown 分隔行(---|---)应被丢弃,不进入 TableStructure。"""
    md = "a | b | c\n--- | --- | ---\n1 | 2 | 3"
    blocks = _parse_plain_content(md, 0, _meta())
    tables = [b for b in blocks if b.label == "table"]
    assert len(tables) == 1
    tbl = tables[0].table
    assert tbl.headers == ["a", "b", "c"]
    assert tbl.rows == [["1", "2", "3"]]


def test_plain_single_pipe_line_not_table():
    """单行含 | 不应误判为表格(需≥2行)。"""
    md = "地址：xx路 | 电话：123"
    blocks = _parse_plain_content(md, 0, _meta())
    tables = [b for b in blocks if b.label == "table"]
    assert len(tables) == 0
    # 回退为 text
    assert all(b.label == "text" for b in blocks)


def test_loc_fallback_branch():
    """含 <|LOC_|> 标记的内容应走 LOC 解析分支。"""
    loc_content = "标题<|LOC_10|><|LOC_20|><|LOC_30|><|LOC_20|><|LOC_30|><|LOC_40|><|LOC_10|><|LOC_40|>"
    blocks = _parse_loc_content(loc_content, 0, _meta())
    assert len(blocks) == 1
    assert blocks[0].content == "标题"
    # bbox 换算为 PDF pt(LOC 0-1000 空间)
    assert len(blocks[0].bbox) == 4
    assert blocks[0].bbox[0] > 0  # x1


def test_loc_markdown_table_keeps_structure_and_bbox():
    """LOC 坐标与 Markdown 表格同时出现时不能把表格降级为普通文本。"""
    loc = "<|LOC_10|><|LOC_20|><|LOC_900|><|LOC_20|><|LOC_900|><|LOC_80|><|LOC_10|><|LOC_80|>"
    content = "\n".join([
        f"名称 | 数量 | 金额{loc}",
        f"--- | --- | ---{loc}",
        f"商品A | 2 | 100{loc}",
    ])
    blocks = _parse_loc_content(content, 0, _meta())
    tables = [block for block in blocks if block.label == "table"]
    assert len(tables) == 1
    assert tables[0].table.headers == ["名称", "数量", "金额"]
    assert tables[0].table.rows == [["商品A", "2", "100"]]
    assert len(tables[0].bbox) == 4
    regions = _normalize_regions(tables, {0: _meta()})
    assert len(regions) == 1
    assert all(0 <= value <= 1 for value in regions[0].bbox)


def test_plain_parses_tab_separated_table():
    content = "名称\t数量\t金额\n商品A\t2\t100\n商品B\t3\t200"
    blocks = _parse_plain_content(content, 0, _meta())
    tables = [block for block in blocks if block.label == "table"]
    assert len(tables) == 1
    assert tables[0].table.rows[1] == ["商品B", "3", "200"]


def test_plain_parses_html_table_and_preserves_surrounding_text():
    content = "合同标题\n<table><tr><th>名称</th><th>金额</th></tr><tr><td>A</td><td>100</td></tr></table>\n合同尾部"
    blocks = _parse_plain_content(content, 0, _meta())
    tables = [block for block in blocks if block.label == "table"]
    texts = [block.content for block in blocks if block.label == "text"]
    assert len(tables) == 1
    assert tables[0].table.headers == ["名称", "金额"]
    assert tables[0].table.rows == [["A", "100"]]
    assert texts == ["合同标题", "合同尾部"]


def test_plain_table_allows_blank_lines_between_rows():
    content = "名称 | 金额\n\n--- | ---\n\nA | 100"
    blocks = _parse_plain_content(content, 0, _meta())
    tables = [block for block in blocks if block.label == "table"]
    assert len(tables) == 1
    assert tables[0].table.rows == [["A", "100"]]


def test_plain_table_pads_uneven_rows():
    table = _plain_text_to_table("名称 | 数量 | 金额\nA | 2")
    assert table.rows == [["A", "2", ""]]


def test_plain_collapses_hallucinated_repeated_table_tail():
    """真实 PaddleOCR 输出会重复同一续行直到 token 上限，应清除重复尾部。"""
    repeated = " | | JL-B 军绿色薄型内径 6mm | | "
    content = "\n".join([
        "序号 | 物料编码 | 产品名称 | 数量 | 金额",
        "--- | --- | --- | --- | ---",
        "1 | YA030000 | 绵纶丝编织套管 | 200 | 250.00",
        *([repeated] * 12),
        " |",
    ])
    blocks = _parse_plain_content(content, 0, _meta())
    table = next(block.table for block in blocks if block.label == "table")
    assert table.rows == [
        ["1", "YA030000", "绵纶丝编织套管", "200", "250.00"],
        ["", "", "JL-B 军绿色薄型内径 6mm", "", ""],
    ]


def test_plain_text_to_table():
    """_plain_text_to_table:首行 headers,其余 rows。"""
    norm = "序号 | 名称\n1 | A\n2 | B"
    tbl = _plain_text_to_table(norm)
    assert tbl is not None
    assert tbl.headers == ["序号", "名称"]
    assert tbl.rows == [["1", "A"], ["2", "B"]]


def test_plain_text_to_table_empty():
    """空文本返回 None。"""
    assert _plain_text_to_table("") is None
    assert _plain_text_to_table("   ") is None


def test_engine_reads_llm_config():
    """paddleocr 后端直接复用 llm_* 配置(无独立 paddleocr_* 字段)。"""
    from document_comparison.ocr.paddleocr_http import PaddleOCREngine

    eng = PaddleOCREngine()
    # 应与 llm_* 配置一致(settings.llm_api_base / llm_api_key / llm_model)
    from document_comparison.config import settings
    assert eng.api_base == settings.llm_api_base
    assert eng.api_key == settings.llm_api_key
    assert eng.model == settings.llm_model
