"""docx 解析日志测试:验证 parse_word 输出的解析摘要日志。

覆盖新增的 `_log_parse_summary`:
- INFO「word parsed ...」记录各类块计数、标题层级、表格规模、字符数、耗时
- 当文档含表格但全部被空跳过时,输出 WARNING

构造方式:用 python-docx 在内存生成 docx,临时落盘后调 parse_word,
与 test_table_structure.py 同一套路,不依赖外部样本文件。
"""
from __future__ import annotations

import logging
from pathlib import Path

from docx import Document

from document_comparison.parsing.word import parse_word


def _make_rich_docx(path: Path) -> None:
    """构造含标题 + 段落 + 表格的混合文档。

    结构:
      - 标题(Heading 1):第一条 合同主体
      - 段落:甲方:... 乙方:... (键值块,但解析层只产 paragraph)
      - 标题(Heading 2):1.1 价款
      - 表格:2 行 × 2 列
      - 空段落(验证 skipped_empty 计数)
    """
    doc = Document()
    doc.add_heading("第一条 合同主体", level=1)
    doc.add_paragraph("甲方:买方公司")
    doc.add_paragraph("乙方:卖方公司")
    doc.add_heading("1.1 价款", level=2)
    t = doc.add_table(rows=2, cols=2)
    t.cell(0, 0).text = "项目"
    t.cell(0, 1).text = "金额"
    t.cell(1, 0).text = "货款"
    t.cell(1, 1).text = "1000元"
    doc.add_paragraph("")  # 空段落,应被跳过并计入 skipped_empty
    doc.save(str(path))


def test_parse_summary_info_logged(tmp_path, caplog):
    """parse_word 应输出含各类块计数与规模的 INFO 摘要日志。"""
    docx_path = tmp_path / "rich.docx"
    _make_rich_docx(docx_path)

    with caplog.at_level(logging.INFO, logger="document_comparison.parsing.word"):
        items = parse_word(str(docx_path))

    summary = next(
        (r for r in caplog.records if r.message.startswith("word parsed")), None
    )
    assert summary is not None, "应输出 word parsed 摘要日志"
    msg = summary.message

    # 文件名(不含路径,避免日志泄露绝对路径)
    assert f"path={docx_path.name}" in msg
    # 各类块计数:2 个标题 + 2 段落 + 1 表格 = 5 项
    assert "items=5" in msg
    assert "headings=2" in msg
    # 段落数取自 doc.paragraphs(python-docx 中标题也计入段落)
    # 2 标题 + 2 键值段 + 1 空段 = 5
    assert "paragraphs=5" in msg
    # 表格规模:rows×headers,rows 仅数据行(表头单独存 headers)
    # 2×2 物理表 = 1 数据行 × 2 列
    assert "table_dims=1x2" in msg
    # 跳过的空段落数
    assert "skipped_empty=1" in msg
    # 耗时字段存在
    assert "elapsed=" in msg
    # 标题层级分布:level 1 与 level 2 各一
    assert "heading_levels=" in msg

    # 返回值本身不受日志影响
    assert len(items) == 5
    assert sum(1 for it in items if it.kind == "table") == 1


def test_empty_table_dropped_warning(tmp_path, caplog):
    """文档含表格但全部为空时,应输出 WARNING 提示。"""
    doc = Document()
    # 全空表格:_table_to_text 返回空串,parse_word 会跳过它
    t = doc.add_table(rows=2, cols=2)
    for r in range(2):
        for c in range(2):
            t.cell(r, c).text = ""
    # 补一个正常段落,使 items 非全空(单纯验证 warning 触发条件)
    doc.add_paragraph("正文")
    docx_path = tmp_path / "empty_table.docx"
    doc.save(str(docx_path))

    with caplog.at_level(logging.WARNING, logger="document_comparison.parsing.word"):
        items = parse_word(str(docx_path))

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("解析后 0 张入库" in r.message for r in warnings), \
        "全空表格应触发 WARNING"

    # 解析结果:只有段落,表格被跳过
    assert len(items) == 1
    assert items[0].kind == "paragraph"
