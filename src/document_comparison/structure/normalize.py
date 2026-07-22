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

# OCR 公式识别(尤其 PaddleOCR-VL 的 useFormulaRecognition)会把中文「着重号」
# (字下方一个圆点,合同里常用于强调甲/乙方)误判为数学下标,吐出 LaTeX 片段。
# 合同文本中不应出现真正的数学公式,以下清洗仅处理 OCR 产生的确定性噪音,
# 对 Word 侧是完全的 no-op(Word 永远不会产生 \underset / \( 这类语法)。
#
# 匹配顺序很重要:先处理「单字符下单点」的常见误判(保留主字符),再处理通用
# \underset,最后剥除残余的 LaTeX 定界符。
# \underset{\cdot}{X} 或 \underset{.}{X} —— X 为单字符(含中文),还原为 X。
_LATEX_UNDERSET_DOT = re.compile(
    r"\\underset\s*\{\s*(?:\\cdot|\.|\u2022|\\bullet)\s*\}\s*\{(?P<base>.)\}"
)
# 通用 \underset{下标}{主字符} —— 保留主字符,丢弃下标。
# 下标允许一层大括号嵌套,以兼容 \underset{\mathrm{注}}{条} 这类内含命令参数的形式。
_LATEX_UNDERSET = re.compile(
    r"\\underset\s*\{(?:[^{}]|\{[^{}]*\})*\}\s*\{(?P<base>.)\}"
)
# 残余 LaTeX 行内/行间定界符:$$ \(\) \[\]。合同正文里无合法用途,直接剥除。
_LATEX_DELIM = re.compile(r"\$\$|\\\(|\\\)|\\\[|\\\]")


def _strip_latex_noise(text: str) -> str:
    """剥离 OCR 公式识别产生的 LaTeX 噪音,还原为纯文字。"""
    if "\\" not in text and "$" not in text:
        return text
    s = _LATEX_UNDERSET_DOT.sub(lambda m: m.group("base"), text)
    s = _LATEX_UNDERSET.sub(lambda m: m.group("base"), s)
    s = _LATEX_DELIM.sub("", s)
    return s


def normalize_text(text: str) -> str:
    if not text:
        return ""
    s = _strip_latex_noise(text)
    s = unicodedata.normalize("NFKC", s)
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
