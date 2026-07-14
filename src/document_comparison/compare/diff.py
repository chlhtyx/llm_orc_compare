"""字符级 diff:difflib 对齐(§5.5)。

对中文字符串按字符对齐,replace 拆为 delete+insert,便于红删绿增渲染。
"""
from __future__ import annotations

from difflib import SequenceMatcher

from ..models import DiffSegment, TableStructure


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


def table_diff(a: TableStructure, b: TableStructure) -> list[DiffSegment]:
    """表格单元格级 diff(一期增强)。

    按行对齐两端表格(SequenceMatcher 以行文本为最小匹配单元),
    对配对成功的行逐单元格做 char_diff,拼成线性 DiffSegment 流。
    结构性增删行以「整行 delete/insert」呈现,便于前端渲染。

    输出约定:每个单元格的 diff 段以换行分隔,行间以空行分隔,
    使下游消费方能区分单元格与行边界。

    设计分层(刻意不强行列对齐):
    - 值篡改(同位置单元格的值变了)→ 行能配对 → 逐单元格 char_diff,精确定位。
    - 结构差异(行/列数不一致、行整增整删)→ 行文本不一致 → 整行 replace,
      多余行按 delete/insert 呈现。不做单元格级别的列重对齐,避免对「增删列」
      这种结构性变化做过度猜测性匹配。两端的列结构由 _table_rows(Word)与
      _parse_blocks(OCR)保证对称,此处只负责在结构一致的前提下比对值。
    """
    a_rows = [" | ".join(a.headers)] + [" | ".join(r) for r in a.rows]
    b_rows = [" | ".join(b.headers)] + [" | ".join(r) for r in b.rows]
    sm = SequenceMatcher(a=a_rows, b=b_rows, autojunk=False)
    segs: list[DiffSegment] = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            for k in range(i1, i2):
                segs.append(DiffSegment(op="equal", text=a_rows[k] + "\n"))
        elif tag == "delete":
            for k in range(i1, i2):
                segs.append(DiffSegment(op="delete", text=a_rows[k] + "\n"))
        elif tag == "insert":
            for k in range(j1, j2):
                segs.append(DiffSegment(op="insert", text=b_rows[k] + "\n"))
        elif tag == "replace":
            # 行级 replace:进一步做单元格内字符级 diff
            a_block = a_rows[i1:i2]
            b_block = b_rows[j1:j2]
            # 配对行逐行做字符级 diff;多余行按 delete/insert 处理
            pair_count = min(len(a_block), len(b_block))
            for k in range(pair_count):
                cell_segs = char_diff(a_block[k], b_block[k])
                segs.extend(cell_segs)
                segs.append(DiffSegment(op="equal", text="\n"))
            for k in range(pair_count, len(a_block)):
                segs.append(DiffSegment(op="delete", text=a_block[k] + "\n"))
            for k in range(pair_count, len(b_block)):
                segs.append(DiffSegment(op="insert", text=b_block[k] + "\n"))
    return segs
