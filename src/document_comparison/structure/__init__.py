"""③ 结构化抽取层:文本归一化与条款切分。"""
from __future__ import annotations

from .clause import build_clauses, blocks_to_raw, detect_field_key
from .normalize import normalize_text

__all__ = ["normalize_text", "build_clauses", "blocks_to_raw", "detect_field_key"]
