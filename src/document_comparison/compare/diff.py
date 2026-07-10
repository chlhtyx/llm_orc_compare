"""字符级 diff:difflib 对齐(§5.5)。

对中文字符串按字符对齐,replace 拆为 delete+insert,便于红删绿增渲染。
"""
from __future__ import annotations

from difflib import SequenceMatcher

from ..models import DiffSegment


def char_diff(a: str, b: str) -> list[DiffSegment]:
    sm = SequenceMatcher(a=a, b=b, autojunk=False)
    segs: list[DiffSegment] = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            segs.append(DiffSegment(op="equal", text=a[i1:i2]))
        elif tag == "delete":
            segs.append(DiffSegment(op="delete", text=a[i1:i2]))
        elif tag == "insert":
            segs.append(DiffSegment(op="insert", text=b[j1:j2]))
        elif tag == "replace":
            if a[i1:i2]:
                segs.append(DiffSegment(op="delete", text=a[i1:i2]))
            if b[j1:j2]:
                segs.append(DiffSegment(op="insert", text=b[j1:j2]))
    return segs


def is_only_whitespace_or_punct(a: str, b: str) -> bool:
    """归一化后字符是否一致(用于判格式噪声)。"""
    return a == b
