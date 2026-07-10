"""⑤ 比对与篡改检测层。"""
from __future__ import annotations

from .diff import char_diff
from .elements import extract_key_elements, elements_changed
from .risk import classify_diff

__all__ = ["char_diff", "extract_key_elements", "elements_changed", "classify_diff"]
