"""§6 数据结构定义(pydantic v2)。

所有跨层传递的结构在此统一定义,字段命名对齐技术方案 §6。
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

# —— 枚举(用 Literal,JSON 友好)——
DocType = Literal["word", "pdf"]
MatchType = Literal["number", "semantic", "unmatched"]
DiffStatus = Literal["identical", "modified", "added", "deleted"]
RiskLevel = Literal["high", "medium", "low", "none"]
OverallRisk = Literal["high", "medium", "low", "clean"]
KeyElementKind = Literal[
    "amount", "date", "ratio", "term", "breach", "jurisdiction", "effective", "seal"
]
BBoxShape = Literal["rect", "quad", "poly"]


class Block(BaseModel):
    """版面元素(多模态 LLM OCR 输出)。"""

    block_id: str
    page_index: int
    label: str = Field(..., description="text/table/doc_title/paragraph_title/seal ...")
    bbox: list[float] = Field(default_factory=list, description="PDF 点坐标(pt) [x1,y1,x2,y2]")
    content: str = ""


class RawItem(BaseModel):
    """解析中间态:Word 段落流与 OCR 版面块统一映射为此类型,再过结构化抽取。

    对应 §5.1(Word)/§5.2(OCR)的原始产出,§5.3 条款切分统一消费。
    """

    text: str
    kind: Literal["paragraph", "heading", "table", "title"] = "paragraph"
    heading_level: int = 0
    page_index: int = 0
    bbox: list[float] = Field(default_factory=list)


class Clause(BaseModel):
    """条款(两端统一结构)。"""

    clause_id: str
    doc_type: DocType
    level: int = 0
    number: str = ""
    title: str = ""
    text: str = ""
    blocks: list[Block] = Field(default_factory=list)


class Alignment(BaseModel):
    """对齐结果。"""

    word_clause_id: str | None = None
    pdf_clause_id: str | None = None
    match_type: MatchType
    similarity: float = 0.0


class DiffSegment(BaseModel):
    """字符级 diff 片段。"""

    op: Literal["equal", "delete", "insert"]
    text: str


class PageRegion(BaseModel):
    """PDF 页面区域(归一化坐标,供前端高亮叠加,见 §5.6)。"""

    page_index: int
    bbox: list[float] = Field(
        ..., description="归一化 [x1,y1,x2,y2],相对页面尺寸 ∈ [0,1]"
    )
    shape: BBoxShape = "rect"
    polygon: list[list[float]] | None = Field(
        default=None, description="quad/poly 的归一化顶点(shape != rect 时)"
    )


class KeyElement(BaseModel):
    """高风险要素。"""

    kind: KeyElementKind
    word_value: str = ""
    pdf_value: str = ""
    changed: bool = False


class Diff(BaseModel):
    """单条款差异。"""

    alignment_id: str
    status: DiffStatus
    segments: list[DiffSegment] = Field(default_factory=list)
    risk_level: RiskLevel = "none"
    risk_reasons: list[str] = Field(default_factory=list)
    page_regions: list[PageRegion] = Field(
        default_factory=list, description="该条款在 PDF 扫描件上的高亮区域"
    )
    number: str = ""
    title: str = ""


class PageMeta(BaseModel):
    """页面元信息(供前端坐标反算与 pdf.js viewport 对齐)。"""

    page_index: int
    width_px: float
    height_px: float
    pdf_width_pt: float = 0.0
    pdf_height_pt: float = 0.0


class TamperReport(BaseModel):
    """比对报告。"""

    source: str
    target: str
    overall_risk: OverallRisk = "clean"
    summary: dict = Field(default_factory=dict)
    diffs: list[Diff] = Field(default_factory=list)
    key_elements: list[KeyElement] = Field(default_factory=list)
    unmatched_clauses: list[Diff] = Field(default_factory=list)
    page_meta: list[PageMeta] = Field(default_factory=list)


# —— API 层 DTO ——
class CompareOptions(BaseModel):
    similarity_identical: float | None = None
    similarity_modified: float | None = None
    enable_llm_judge: bool = False


TaskStatus = Literal["pending", "running", "done", "failed"]


class TaskInfo(BaseModel):
    task_id: str
    status: TaskStatus = "pending"
    stage: str = ""
    progress: float = 0.0
    overall_risk: OverallRisk | None = None
    error: str | None = None
    elapsed: float | None = None
