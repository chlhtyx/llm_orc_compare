"""§6 数据结构定义(pydantic v2)。

所有跨层传递的结构在此统一定义,字段命名对齐技术方案 §6。
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

# —— 枚举(用 Literal,JSON 友好)——
DocType = Literal["word", "pdf"]
MatchType = Literal["number", "field", "normalized_exact", "semantic", "unmatched"]
DiffStatus = Literal["identical", "modified", "added", "deleted"]
RiskLevel = Literal["high", "medium", "low", "none"]
OverallRisk = Literal["high", "medium", "low", "clean", "needs_review"]
RecognitionStatus = Literal["reliable", "needs_review"]
ChangeVerdict = Literal["clean", "changed", "needs_review"]
EvidenceConfidence = Literal["high", "medium", "low"]
KeyElementKind = Literal[
    "amount",
    "date",
    "ratio",
    "term",
    "breach",
    "jurisdiction",
    "effective",
    "seal",
    "party",
    "account",
    "identifier",
    "negation",
]
BBoxShape = Literal["rect", "quad", "poly"]


class TableStructure(BaseModel):
    """结构化表格(用于单元格级比对)。

    label=table 的 Block 在保留 content 纯文本(向后兼容)的同时,
    额外携带结构化表头与行数据,使 Diff 引擎能定位到具体单元格。
    """

    headers: list[str] = Field(default_factory=list, description="表头各列名称")
    rows: list[list[str]] = Field(default_factory=list, description="数据行,每行单元格数应与 headers 对齐")


class Block(BaseModel):
    """版面元素(多模态 LLM OCR 输出)。"""

    block_id: str
    page_index: int
    label: str = Field(..., description="text/table/doc_title/paragraph_title/seal ...")
    bbox: list[float] = Field(default_factory=list, description="PDF 点坐标(pt) [x1,y1,x2,y2]")
    content: str = ""
    table: TableStructure | None = Field(default=None, description="label=table 时的结构化表头/行;None 表示非表格或未结构化")


class RawItem(BaseModel):
    """解析中间态:Word 段落流与 OCR 版面块统一映射为此类型,再过结构化抽取。

    对应 §5.1(Word)/§5.2(OCR)的原始产出,§5.3 条款切分统一消费。
    """

    text: str
    kind: Literal["paragraph", "heading", "table", "title"] = "paragraph"
    heading_level: int = 0
    page_index: int = 0
    bbox: list[float] = Field(default_factory=list)
    field_key: str = ""
    table: TableStructure | None = Field(default=None, description="kind=table 时的结构化表头/行")


class Clause(BaseModel):
    """条款(两端统一结构)。"""

    clause_id: str
    doc_type: DocType
    level: int = 0
    number: str = ""
    title: str = ""
    text: str = ""
    blocks: list[Block] = Field(default_factory=list)
    tables: list[TableStructure] = Field(
        default_factory=list,
        description="条款内含的结构化表格(来自 table 块),供单元格级比对",
    )
    field_key: str = Field(default="", description="键值块的字段名锚点(如甲方/乙方/地址/日期),供对齐层字段锚定;非键值块为空")


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
    # 表格要素定位:标识要素来自结构化表格的哪一行哪一列,便于前端高亮到具体单元格。
    # 非表格要素为空/0,表示来自正文文本。
    row_index: int = Field(default=-1, description="结构化表格中的行号(0基,-1 表示非表格)")
    col_header: str = Field(default="", description="结构化表格中的列名(空表示非表格)")


class Diff(BaseModel):
    """单条款差异。"""

    alignment_id: str
    status: DiffStatus
    segments: list[DiffSegment] = Field(default_factory=list)
    risk_level: RiskLevel = "none"
    risk_reasons: list[str] = Field(default_factory=list)
    verdict: ChangeVerdict = Field(
        default="changed",
        description="变化裁决；与业务严重度 risk_level、证据可信度 confidence 分离",
    )
    confidence: EvidenceConfidence = Field(
        default="high",
        description="当前差异证据的可信度；低可信差异必须人工复核",
    )
    judged_by: Literal["rule", "llm"] = Field(
        default="rule", description="严重度说明来源:rule=确定性规则,llm=LLM 辅助说明"
    )
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


class PageRecognitionDiagnostic(BaseModel):
    """逐页读取质量诊断。

    source 记录页面实际采用的读取路径；reliable=False 表示识别内容只能作为
    人工复核线索，不能据此给出确定性的高风险结论。
    """

    page_index: int
    source: Literal["native", "fallback"]
    reliable: bool = True
    reasons: list[str] = Field(default_factory=list)
    char_count: int = 0
    table_count: int = 0


class TamperReport(BaseModel):
    """比对报告。"""

    source: str
    target: str
    overall_risk: OverallRisk = "clean"
    change_status: ChangeVerdict = Field(
        default="clean",
        description="整份文档裁决；changed 与 needs_review 均不允许自动通过",
    )
    summary: dict = Field(default_factory=dict)
    diffs: list[Diff] = Field(default_factory=list)
    key_elements: list[KeyElement] = Field(default_factory=list)
    unmatched_clauses: list[Diff] = Field(default_factory=list)
    page_meta: list[PageMeta] = Field(default_factory=list)
    recognition_status: RecognitionStatus = "reliable"
    recognition_diagnostics: list[PageRecognitionDiagnostic] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _infer_legacy_change_status(cls, data):
        """旧报告没有 change_status；加载时从已有差异和识别状态安全推断。"""
        if not isinstance(data, dict) or "change_status" in data:
            return data
        inferred = dict(data)
        if inferred.get("recognition_status") == "needs_review":
            inferred["change_status"] = "needs_review"
        elif inferred.get("diffs") or inferred.get("unmatched_clauses"):
            inferred["change_status"] = "changed"
        else:
            inferred["change_status"] = "clean"
        return inferred


# —— 无标注版(纯文本 difflib 比对)数据结构 ——
# 与上面的 TamperReport 体系完全独立,不经过条款对齐/风险分级,
# 仅做 Word→纯文本、PDF→纯文本、difflib 行级比对。
class TextDiffHunk(BaseModel):
    """一段差异(含少量上下文)。

    tag 来自 difflib 的 opcode,equal 不入库(只在 context_* 里作上下文)。
    """

    tag: Literal["replace", "delete", "insert"]
    word_lines: list[str] = Field(default_factory=list, description="Word 侧差异行(delete/replace)")
    pdf_lines: list[str] = Field(default_factory=list, description="PDF 侧差异行(insert/replace)")
    char_segments: list[DiffSegment] = Field(
        default_factory=list,
        description="仅 tag=replace 时:行内字符级 diff(复用 DiffSegment)",
    )
    context_before: list[str] = Field(default_factory=list, description="差异前的上下文行(equal)")
    context_after: list[str] = Field(default_factory=list, description="差异后的上下文行(equal)")


class TextDiffReport(BaseModel):
    """纯文本 difflib 比对报告(与 TamperReport 完全独立)。"""

    source: str
    target: str
    word_text: str = ""
    pdf_text: str = ""
    hunks: list[TextDiffHunk] = Field(default_factory=list)
    stats: dict = Field(default_factory=dict)
    recognition_status: RecognitionStatus = "reliable"
    recognition_diagnostics: list[PageRecognitionDiagnostic] = Field(default_factory=list)


# —— API 层 DTO ——
class CompareOptions(BaseModel):
    # 兼容旧客户端；零容忍模式中不再参与变化裁决，前端已移除。
    similarity_identical: float | None = Field(default=None, deprecated=True)
    similarity_modified: float | None = Field(default=None, deprecated=True)
    enable_llm_judge: bool = False
    # OCR 引擎选择(每次提交时由对比页选择);None 表示用默认 llm。
    ocr_backend: Literal["llm", "paddleocr"] | None = None


class RawCompareOptions(BaseModel):
    """无标注版提交选项(LLM 整篇比对流程)。"""

    char_level: bool = Field(
        default=False,
        description="replace 行是否做字符级细化(红/绿标记);默认关闭以降低 LLM 输出量与超时风险",
    )
    # OCR 引擎选择(每次提交时由对比页选择);None 表示用默认 llm。
    ocr_backend: Literal["llm", "paddleocr"] | None = None


# —— 对帐单金额统计(独立通道,与 TamperReport / TextDiffReport 完全隔离)——
# 一次可上传多个 PDF,串行 OCR + 表格抽取 + 代码确定性求和,聚合输出总金额。
# LLM 仅在启发式列定位失败时指认金额列(列索引+角色),绝不参与数值识别或求和。
class StatementAmountItem(BaseModel):
    """单行金额明细(可审计粒度)。"""

    file_index: int = Field(default=0, description="所属 PDF 在本次提交中的序号")
    file_name: str = ""
    table_index: int = Field(default=0, description="该 PDF 内的表格序号")
    page_index: int = Field(default=0, description="表格所在页(0 基)")
    row_index: int = Field(default=0, description="表格内行号(0 基)")
    row_label: str = Field(default="", description="首列单元格内容,用于标识本行")
    column: str = Field(default="", description="金额列名(启发式或 LLM 指认)")
    raw_cell: str = Field(default="", description="OCR 原始单元格文本")
    canonical: str = Field(default="", description='canonical 形式,如 "CNY:30000.00"')
    value: float = Field(default=0.0, description="解析后数值(单位元)")


class StatementTableSummary(BaseModel):
    """单张表格的统计结果。"""

    file_index: int = 0
    file_name: str = ""
    table_index: int = 0
    page_index: int = 0
    headers: list[str] = Field(default_factory=list)
    # 按金额列分别求和(支持"已付/未付/金额"等多金额列)
    column_sums: dict[str, float] = Field(default_factory=dict)
    # 表内"合计/小计"行声明的值(按列)
    declared_totals: dict[str, float] = Field(default_factory=dict)
    # column_sums vs declared_totals 是否一致(同列存在时才比较)
    totals_match: dict[str, bool] = Field(default_factory=dict)
    items: list[StatementAmountItem] = Field(default_factory=list)
    skipped_rows: list[int] = Field(default_factory=list, description="被跳过的行号(合计行本身,避免重复计入)")
    # 列定位方式:heuristic(启发式) | llm(LLM 兜底) | none(定位失败)
    column_source: dict[str, Literal["heuristic", "llm", "none"]] = Field(default_factory=dict)


class StatementFileSummary(BaseModel):
    """单个 PDF 的统计汇总。"""

    file_index: int = 0
    file_name: str = ""
    total_amount: float = Field(default=0.0, description="本 PDF 所有金额列之和")
    tables: list[StatementTableSummary] = Field(default_factory=list)
    recognition_status: RecognitionStatus = "reliable"
    recognition_diagnostics: list[PageRecognitionDiagnostic] = Field(default_factory=list)
    error: str | None = Field(default=None, description="单文件失败原因(其他文件继续处理)")


class StatementSummaryReport(BaseModel):
    """对帐单金额统计报告(多文件聚合)。"""

    files: list[StatementFileSummary] = Field(default_factory=list)
    grand_total: float = Field(default=0.0, description="所有文件所有金额列之和")
    grand_totals_by_column: dict[str, float] = Field(
        default_factory=dict, description="跨文件按列名汇总"
    )
    total_files: int = 0
    total_tables: int = 0
    total_items: int = 0
    verdict: ChangeVerdict = Field(
        default="clean",
        description="clean / changed(声明不一致) / needs_review(OCR 或列定位低置信)",
    )
    reasons: list[str] = Field(default_factory=list, description="判定理由(可审计)")
    column_detection_summary: dict[str, int] = Field(
        default_factory=dict,
        description='{"heuristic": N, "llm": M, "none": K}',
    )


class StatementOptions(BaseModel):
    """对帐单统计提交选项。"""

    ocr_backend: Literal["llm", "paddleocr"] | None = None
    # 用户自定义金额列关键词(覆盖默认启发式表)
    amount_column_keywords: list[str] | None = None
    # 启用 LLM 列定位兜底(默认开启)
    enable_llm_column_detection: bool = True


TaskStatus = Literal["pending", "running", "done", "failed"]


class TaskInfo(BaseModel):
    task_id: str
    status: TaskStatus = "pending"
    stage: str = ""
    progress: float = 0.0
    stage_timings: dict[str, float] = Field(
        default_factory=dict,
        description="已完成处理阶段的耗时秒数，键为 word/ocr/structure/align/compare 等",
    )
    overall_risk: OverallRisk | None = None
    error: str | None = None
    elapsed: float | None = None
