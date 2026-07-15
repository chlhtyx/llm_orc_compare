"""⑤ 比对与篡改检测层。"""
from __future__ import annotations

from .adjudication import adjudicate_clause_pair
from .diff import char_diff
from .elements import (
    canonicalize_contract_text,
    extract_key_elements,
    elements_changed,
    reviewable_formatting_change,
)
from .risk import classify_diff

__all__ = [
    "adjudicate_clause_pair",
    "canonicalize_contract_text",
    "char_diff",
    "extract_key_elements",
    "elements_changed",
    "reviewable_formatting_change",
    "classify_diff",
]
