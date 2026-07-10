"""文本归一化:全半角统一、标点统一、空白清理(§5.3 步骤 1)。

NFKC 把全角字母/数字转为半角,并统一部分兼容字符;
行内空白合并为单空格,但**保留换行**(换行是条款边界结构信息)。
"""
from __future__ import annotations

import re
import unicodedata

_INLINE_WS = re.compile(r"[^\S\n]+")
_MULTI_NL = re.compile(r"\n{2,}")


def normalize_text(text: str) -> str:
    if not text:
        return ""
    s = unicodedata.normalize("NFKC", text)
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    s = _INLINE_WS.sub(" ", s)  # 行内空白(含全角空格)→ 单空格
    s = _MULTI_NL.sub("\n", s)  # 多换行合并
    return s.strip()
