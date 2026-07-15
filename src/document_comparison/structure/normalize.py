"""文本归一化:全半角统一、标点统一、空白清理(§5.3 步骤 1)。

NFKC 把全角字母/数字转为半角,并统一部分兼容字符;
行内空白合并为单空格,但**保留换行**(换行是条款边界结构信息)。
"""
from __future__ import annotations

import re
import unicodedata

_INLINE_WS = re.compile(r"[^\S\n]+")
_MULTI_NL = re.compile(r"\n{2,}")

# Markdown 表格分隔行:仅由 : - | 与空白构成(如 |---|:--:|---|)。
_MD_SEP_LINE = re.compile(r"^[\s:|\-]+$")
# 单元格分隔:竖线或制表符。竖线前后的空白会在拆分后单独折叠。
_CELL_SPLIT = re.compile(r"\s*\|\s*|\t")


def normalize_text(text: str) -> str:
    if not text:
        return ""
    s = unicodedata.normalize("NFKC", text)
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    s = _INLINE_WS.sub(" ", s)  # 行内空白(含全角空格)→ 单空格
    s = _MULTI_NL.sub("\n", s)  # 多换行合并
    return s.strip()


def normalize_table_text(text: str) -> str:
    """把任意格式的表格文本归一化为规范形式:每行「单元格 | 单元格 | ...」。

    消除 Word 侧(` | ` 连接)与 OCR 侧(Markdown 表格 / TSV / 首尾包裹管线)
    表达同一张表时的格式差异——这种差异会让 difflib 产生大量伪差异
    (整张表被判 replace/insert,相似度掉到 0)。

    规范化规则(逐行处理):
    - 丢弃 Markdown 分隔行(`|---|---|`、`:---:` 等仅含 `: - | 空白` 的行)。
    - 去掉行首/行尾包裹的 `|`(Markdown 管线),再按 `|` 或制表符拆分为单元格。
    - 单元格内空白折叠为单空格;丢弃首尾产生的空单元格(悬挂管线残留)。
    - 用 `" | "` 重新连接单元格。

    幂等:已是规范 `cell | cell` 格式时原样返回(等价),故 Word 侧套用是安全 no-op,
    且保证两端对称——无论哪侧格式变动,过一遍此函数即可对齐。

    边界:若输入是「逐单元格一行」(每个单元格独占一行),无法可靠重建行结构,
    此函数会原样保留各行(各视为单格行)。需在 OCR prompt 层要求按行输出以规避。
    """
    if not text:
        return ""
    out_lines: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        # 丢弃 Markdown 分隔行(|---|---|、:---: 等);注意至少要含一个 '-',
        # 避免把仅含 '|' 的空行误判(纯 '|' 不算分隔行)。
        if _MD_SEP_LINE.match(line) and "-" in line:
            continue
        # 去掉首尾包裹的 |(Markdown 管线),例如 "| a | b |" → "a | b"
        if line.startswith("|"):
            line = line[1:].lstrip()
        if line.endswith("|"):
            line = line[:-1].rstrip()
        # 按 | 或制表符拆分;每段空白折叠为单空格
        cells = [_INLINE_WS.sub(" ", c).strip() for c in _CELL_SPLIT.split(line)]
        # 丢弃首尾因悬挂管线产生的空单元格
        while cells and cells[0] == "":
            cells.pop(0)
        while cells and cells[-1] == "":
            cells.pop()
        if not cells:
            continue
        out_lines.append(" | ".join(cells))
    return "\n".join(out_lines)
