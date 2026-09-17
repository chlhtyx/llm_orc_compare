"""供应商 PDF 尾部工程图识别。

这里只允许从文档末尾向前连续排除高置信度工程图。遇到合同正文、签章页或
无法确定的页面立即停止，避免为了减少噪声而误删真实合同内容。
"""
from __future__ import annotations

from dataclasses import dataclass, field
import logging
from pathlib import Path
import re
from typing import Callable

try:
    import pymupdf as fitz
except ImportError:  # pragma: no cover
    import fitz  # type: ignore[no-redef]

from .pdf import render_page


logger = logging.getLogger(__name__)


_CONTRACT_RE = re.compile(
    r"合同(?:编号|正文)?|第[一二三四五六七八九十百千零〇\d]+条|"
    r"甲方|乙方|供方|需方|买方|卖方|违约责任|争议解决|"
    r"签字|签章|盖章|法定代表人|授权代表|签订日期"
)
_DRAWING_TERMS = (
    "图号",
    "图名",
    "比例",
    "制图",
    "设计",
    "审核",
    "校对",
    "版本",
)


@dataclass(frozen=True)
class PageDrawingAssessment:
    """单页分类证据；page_number 使用用户可见的 1 基页码。"""

    page_number: int
    page_type: str
    confidence: float
    signals: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class TrailingDrawingDecision:
    total_pages: int
    body_end_page: int
    excluded_page_numbers: list[int] = field(default_factory=list)
    assessments: list[PageDrawingAssessment] = field(default_factory=list)

    @property
    def confidence(self) -> float | None:
        if not self.assessments:
            return None
        return min(item.confidence for item in self.assessments)


DrawingPageClassifier = Callable[[bytes, str, int], PageDrawingAssessment]


def get_configured_drawing_classifier() -> DrawingPageClassifier | None:
    """复用当前通用 VL 配置；未配置时返回 None，规则层安全保留页面。"""
    from ..config import settings

    if not (settings.llm_api_base or "").strip() or not (settings.llm_model or "").strip():
        return None
    from ..ocr.llm import LLMOCREngine

    engine = LLMOCREngine()

    def _classify(
        image_bytes: bytes,
        extracted_text: str,
        page_number: int,
    ) -> PageDrawingAssessment:
        result = engine.classify_drawing_page(
            image_bytes,
            page_number=page_number,
            extracted_text=extracted_text,
        )
        return PageDrawingAssessment(
            page_number=page_number,
            page_type=result["page_type"],
            confidence=result["confidence"],
            signals=[f"视觉模型:{signal}" for signal in result["signals"]],
        )

    return _classify


def detect_trailing_drawings(
    pdf_path: str | Path,
    *,
    confidence_threshold: float = 0.90,
    classifier: DrawingPageClassifier | None = None,
) -> TrailingDrawingDecision:
    """识别并返回连续尾部工程图；不确定页面一律保留。"""
    assessments: list[PageDrawingAssessment] = []
    with fitz.open(str(pdf_path)) as document:
        total_pages = len(document)
        for page_index in range(total_pages - 1, -1, -1):
            assessment = _assess_page(document[page_index], page_index + 1)
            if assessment.page_type == "unknown" and classifier is not None:
                try:
                    assessment = classifier(
                        render_page(pdf_path, page_index, dpi=120),
                        (document[page_index].get_text("text") or "").strip(),
                        page_index + 1,
                    )
                except Exception as exc:  # noqa: BLE001 - 模型失败必须安全保留页面
                    logger.warning(
                        "drawing visual classification failed page=%s: %s",
                        page_index + 1,
                        exc,
                    )
            if (
                assessment.page_type != "engineering_drawing"
                or assessment.confidence < confidence_threshold
            ):
                break
            assessments.append(assessment)

    excluded = sorted(item.page_number for item in assessments)
    return TrailingDrawingDecision(
        total_pages=total_pages,
        body_end_page=total_pages - len(excluded),
        excluded_page_numbers=excluded,
        assessments=list(reversed(assessments)),
    )


def _assess_page(page, page_number: int) -> PageDrawingAssessment:
    text = "".join((page.get_text("text") or "").split())
    contract_terms = sorted(set(_CONTRACT_RE.findall(text)))
    if contract_terms:
        return PageDrawingAssessment(
            page_number=page_number,
            page_type="contract_body",
            confidence=0.99,
            signals=[f"合同保护词:{'/'.join(contract_terms[:4])}"],
        )

    drawing_terms = [term for term in _DRAWING_TERMS if term in text]
    try:
        vector_items = sum(
            len(path.get("items") or []) for path in page.get_drawings()
        )
    except Exception:  # pragma: no cover - 损坏页/旧 PyMuPDF 安全降级
        vector_items = 0
    landscape = page.rect.width > page.rect.height

    signals: list[str] = []
    if drawing_terms:
        signals.append(f"图纸标题栏:{'/'.join(drawing_terms)}")
    if vector_items:
        signals.append(f"矢量线框:{vector_items}")
    if landscape:
        signals.append("横向页面")

    # 标题栏至少命中三个不同字段，并由线框或横向版式提供第二类独立证据。
    if len(drawing_terms) >= 3 and (vector_items >= 12 or landscape):
        confidence = min(
            0.99,
            0.84 + min(len(drawing_terms), 6) * 0.02 + min(vector_items, 40) * 0.002,
        )
        return PageDrawingAssessment(
            page_number=page_number,
            page_type="engineering_drawing",
            confidence=confidence,
            signals=signals,
        )

    return PageDrawingAssessment(
        page_number=page_number,
        page_type="unknown",
        confidence=0.0,
        signals=signals or ["无足够分类证据"],
    )


__all__ = [
    "PageDrawingAssessment",
    "DrawingPageClassifier",
    "get_configured_drawing_classifier",
    "TrailingDrawingDecision",
    "detect_trailing_drawings",
]
