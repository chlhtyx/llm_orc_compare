"""比对流水线编排:① → ⑥ 全链路(§3)。

run_pipeline(word_path, pdf_path) 跑通:
Word 解析 → 条款切分;PDF 渲染 → OCR → 条款切分;对齐;比对;报告。
"""
from __future__ import annotations

from pathlib import Path

from .align import align_clauses
from .config import Settings, settings
from .embed import get_embed_engine
from .models import TamperReport
from .ocr import get_ocr_engine
from .parsing import get_page_metas, parse_word
from .report import build_report
from .structure import blocks_to_raw, build_clauses


def run_pipeline(
    word_path: str | Path,
    pdf_path: str | Path,
    cfg: Settings | None = None,
    ocr=None,
    embed=None,
) -> TamperReport:
    cfg = cfg or settings
    ocr = ocr or get_ocr_engine(cfg.ocr_backend)
    embed = embed or get_embed_engine(cfg.embed_backend)
    thresholds = {"identical": cfg.similarity_identical, "modified": cfg.similarity_modified}

    # —— ① Word 解析 + ③ 切分 ——
    word_raw = parse_word(word_path)
    word_clauses = build_clauses(word_raw, "word")

    # —— ① PDF + ② OCR + ③ 切分 ——
    page_metas = get_page_metas(pdf_path, cfg.pdf_render_dpi)
    pages_blocks = ocr.recognize(Path(pdf_path), page_metas)
    pdf_raw = blocks_to_raw(pages_blocks)
    pdf_clauses = build_clauses(pdf_raw, "pdf")

    # —— ④ 对齐 ——
    alignments = align_clauses(
        word_clauses, pdf_clauses, embed, cfg.align_similarity
    )

    # —— ⑤⑥ 比对 + 报告 ——
    word_by = {c.clause_id: c for c in word_clauses}
    pdf_by = {c.clause_id: c for c in pdf_clauses}
    return build_report(
        alignments=alignments,
        word_by=word_by,
        pdf_by=pdf_by,
        embed=embed,
        page_metas=page_metas,
        thresholds=thresholds,
        source=str(word_path),
        target=str(pdf_path),
    )
