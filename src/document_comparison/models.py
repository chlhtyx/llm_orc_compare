"""§6 数据结构定义(pydantic v2)。

所有跨层传递的结构在此统一定义,字段命名对齐技术方案 §6。
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

# —— 枚举(用 Literal,JSON 友好)——
DocType = Literal["word", "pdf"]
MatchType = Literal["number", "field", "normalized_exact", "semantic", "llm", "unmatched"]
DiffStatus = Literal["identical", "modified", "added", "deleted"]
RiskLevel = Literal["high", "medium", "low", "none"]
OverallRisk = Literal["high", "medium", "low", "changed", "clean", "needs_review"]
RecognitionStatus = Literal["reliable", "needs_review"]
LocationStatus = Literal["complete", "partial", "missing"]
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
RegionKind = Literal["real", "placeholder"]


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


class RawAlignmentBlock(BaseModel):
    """交给联合分段对齐器的只读原始块视图。"""

    block_id: str
    text: str
    kind: Literal["paragraph", "heading", "table", "title"] = "paragraph"
    page_index: int = 0
    bbox: list[float] = Field(default_factory=list)


class RawSpan(BaseModel):
    """原始块内的半开字符区间 ``[start, end)``。"""

    block_id: str
    start: int = Field(ge=0)
    end: int = Field(gt=0)


class RawAlignmentGroup(BaseModel):
    """联合分段后的一组 Word/PDF 对应区间；允许一侧为空表示增删。"""

    word_spans: list[RawSpan] = Field(default_factory=list)
    pdf_spans: list[RawSpan] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str = ""

    @model_validator(mode="after")
    def _require_one_side(self) -> "RawAlignmentGroup":
        if not self.word_spans and not self.pdf_spans:
            raise ValueError("raw alignment group 至少需要一侧包含 span")
        return self


class RawAlignmentPlan(BaseModel):
    """LLM 返回的整段联合分段与对齐计划。"""

    groups: list[RawAlignmentGroup] = Field(default_factory=list)


class Clause(BaseModel):
    """条款(两端统一结构)。"""

    clause_id: str
    doc_type: DocType
    level: int = 0
    number: str = ""
    title: str = ""
    text: str = ""
    source_page_index: int | None = Field(
        default=None,
        description="原始文档中的页序线索(仅 Word 条款);用于 deleted 占位框跨页时避免错误归页",
    )
    blocks: list[Block] = Field(default_factory=list)
    tables: list[TableStructure] = Field(
        default_factory=list,
        description="条款内含的结构化表格(来自 table 块),供单元格级比对",
    )
    field_key: str = Field(default="", description="键值块的字段名锚点(如甲方/乙方/地址/日期),供对齐层字段锚定;非键值块为空")
    parent_path: list[str] = Field(
        default_factory=list,
        description="从外到内的父章节路径,用于约束重复编号/字段候选的对齐范围",
    )


class Alignment(BaseModel):
    """对齐结果。"""

    word_clause_id: str | None = None
    pdf_clause_id: str | None = None
    word_clause_ids: list[str] = Field(
        default_factory=list,
        description="参与本次对齐的 Word 条款 ID；支持 1↔N，旧报告为空时使用 word_clause_id",
    )
    pdf_clause_ids: list[str] = Field(
        default_factory=list,
        description="参与本次对齐的 PDF 条款 ID；支持 N↔1，旧报告为空时使用 pdf_clause_id",
    )
    match_type: MatchType
    similarity: float = 0.0
    alignment_reason: str = Field(
        default="",
        description="未对齐时记录最近候选、阈值判断和主要字符差异；已对齐时为空",
    )

    @model_validator(mode="after")
    def _sync_clause_ids(self) -> "Alignment":
        """兼容旧单 ID 报告，并让新分组对齐始终保留首个单 ID。"""
        if not self.word_clause_ids and self.word_clause_id:
            self.word_clause_ids = [self.word_clause_id]
        if not self.pdf_clause_ids and self.pdf_clause_id:
            self.pdf_clause_ids = [self.pdf_clause_id]
        if self.word_clause_id is None and self.word_clause_ids:
            self.word_clause_id = self.word_clause_ids[0]
        if self.pdf_clause_id is None and self.pdf_clause_ids:
            self.pdf_clause_id = self.pdf_clause_ids[0]
        return self


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
    kind: RegionKind = Field(
        default="real",
        description=(
            "real=OCR/解析得到的真实高亮区域;"
            "placeholder=推断占位框(deleted 条款在回收件无对应内容,"
            "位置由相邻已配对条款插值得出,仅表示「按文档顺序应在此处附近」)"
        ),
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
        default_factory=list, description="该条款在回收件 PDF 上的高亮区域"
    )
    source_page_regions: list[PageRegion] = Field(
        default_factory=list,
        description="该条款在原件 PDF 或 DOCX 派生 PDF 上经真实文本块验证的高亮区域",
    )
    alignment_reason: str = Field(
        default="",
        description="未对齐原因；旧报告及已对齐条款为空",
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
    location_status: LocationStatus = "complete"
    bbox_coverage: float = Field(
        default=1.0,
        ge=0.0,
        le=1.0,
        description="该页有效 OCR 字符中具备 PDF 坐标的比例；不参与内容变化裁决",
    )


class PageScopeDecision(BaseModel):
    """自动正文边界识别中，被排除页面的可审计判定证据。"""

    page_number: int = Field(ge=1, description="用户可见的 1 基页码")
    page_type: Literal["contract_body", "engineering_drawing", "unknown"]
    confidence: float = Field(ge=0.0, le=1.0)
    signals: list[str] = Field(default_factory=list)


class TruncationRecord(BaseModel):
    """回收件正文范围截取记录(等保审计留痕)。

    仅在手工边界、自动尾部图纸识别或兼容的原件页数模式真正移除页面时填充；
    未移除页面时 TamperReport.truncation 为 None。记录截取原因、排除页码与
    自动识别证据，使“哪些页面未进入比对、为何排除”可追溯。
    """

    original_pdf_page_count: int = Field(description="截断前回收 PDF 实际页数")
    truncated_pdf_page_count: int = Field(description="截断后参与比对的 PDF 页数")
    original_doc_page_count: int | None = Field(
        default=None,
        description="原始合同页数；按目标正文边界截取时为空",
    )
    doc_page_count_source: Literal["estimated", "explicit"] | None = Field(
        default=None,
        description="原始合同页数来源:estimated=OOXML 估算,explicit=外部显式传入",
    )
    truncation_reason: Literal[
        "original_page_count", "manual_target_body_end_page", "auto_trailing_drawings"
    ] = Field(
        default="original_page_count",
        description="截取原因；旧报告默认按原件页数截取",
    )
    excluded_page_numbers: list[int] = Field(
        default_factory=list,
        description="未参与比对的回收件页码，按用户可见的 1 基页码记录",
    )
    detection_confidence: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="自动尾部图纸识别的最低页级置信度",
    )
    page_decisions: list[PageScopeDecision] = Field(
        default_factory=list,
        description="自动排除页的分类、置信度与判定证据",
    )


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
    # 保持 page_meta 的既有语义：它始终属于回收件 PDF。
    page_meta: list[PageMeta] = Field(default_factory=list)
    source_page_meta: list[PageMeta] = Field(
        default_factory=list,
        description="原件 PDF 或 DOCX 派生 PDF 的页面元信息",
    )
    source_annotation_status: Literal["available", "partial", "unavailable"] = Field(
        default="unavailable",
        description="原件侧是否可生成视觉标注；仅原件 PDF 且取得页元数据时 available",
    )
    source_annotation_reason: str = Field(
        default="",
        description="原件侧无法标注时的原因；不影响内容比对结论",
    )
    truncation: TruncationRecord | None = Field(
        default=None,
        description="回收件页数截取记录;None 表示未发生截断(等保审计留痕)",
    )
    recognition_status: RecognitionStatus = "reliable"
    recognition_diagnostics: list[PageRecognitionDiagnostic] = Field(default_factory=list)
    location_status: LocationStatus = Field(
        default="complete",
        description="PDF 高亮定位完整度；与 OCR 内容可靠性及变化裁决相互独立",
    )

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
    # 开启后优先让纯文本 LLM 基于 DOCX 段落/PDF OCR block 的字符区间联合
    # 分段并对齐；整份计划校验失败时回退 Clause 对齐与受限候选裁决。
    # 最终字符/字段/表格变化始终由确定性裁决。
    enable_llm_alignment: bool = False
    # 是否启用风险判别(高风险要素抽取 + 严重度分级 + LLM 辅助说明)。
    # 默认 False:仅列举字符级/表格级差异,不做风险判定、不抽取高风险要素。
    # True 时恢复完整风险分级行为(向后兼容)。
    enable_risk_assessment: bool = False
    # LLM 直接比对:开启后跳过条款切分/对齐/裁决,把 Word 与 PDF 各自解析成
    # 纯文本后直接交给 LLM 比对差异并标注(复用无标注版管线 + llm_text_diff)。
    # 结果适配为标准 TamperReport;LLM 仅做语义差异,不做风险分级
    # (per-diff risk_level 恒 none)。无 PDF 坐标 -> 报告页不渲染高亮框。
    enable_llm_direct_diff: bool = False
    # OCR 引擎选择(每次提交时由对比页选择);None 表示用默认 llm。
    ocr_backend: Literal["llm", "paddleocr"] | None = None
    # 回收件页数截取:开启后,回收 PDF 页数超过原始合同时,截取到原始页数再比对。
    # 用物理截断(生成前 N 页子集 PDF)保证 OCR/报告/高亮图在页数维度一致。
    truncate_to_original_pages: bool = False
    # 自动仅排除供应商 PDF 连续尾部的高置信度工程图；正文、签章页与不确定页保留。
    auto_discard_trailing_drawings: bool = False
    # 供应商 PDF 正文实际截止页(1 基)。提供时优先于自动识别和旧原件页数截取。
    target_body_end_page: int | None = None
    # 原始合同页数(可选显式覆盖);不传时按 docx OOXML 分页符估算。
    # 外部系统已知真实页数时应优先传此值,避免渲染器差异导致的估算偏差。
    original_page_count: int | None = None

    @field_validator("original_page_count")
    @classmethod
    def _check_original_page_count(cls, v: int | None) -> int | None:
        if v is not None and v < 1:
            raise ValueError("original_page_count 必须 >= 1")
        return v

    @field_validator("target_body_end_page")
    @classmethod
    def _check_target_body_end_page(cls, v: int | None) -> int | None:
        if v is not None and v < 1:
            raise ValueError("target_body_end_page 必须 >= 1")
        return v


class RawCompareOptions(BaseModel):
    """无标注版提交选项(LLM 整篇比对流程)。"""

    char_level: bool = Field(
        default=False,
        description="replace 行是否做字符级细化(红/绿标记);默认关闭以降低 LLM 输出量与超时风险",
    )
    # OCR 引擎选择(每次提交时由对比页选择);None 表示用默认 llm。
    ocr_backend: Literal["llm", "paddleocr"] | None = None


# —— 金额统计(独立通道,与 TamperReport / TextDiffReport 完全隔离)——
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
    # 本表含税金额合计(代码确定性算术,见 amount_column.summarize_table 三种 Case)
    tax_inclusive_total: float = 0.0
    # 含税口径来源:如「含税/价税合计列」「金额(不含税)列 + 税额列」「金额列」「LLM 抽取」
    tax_inclusive_method: str = ""


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
    """金额统计报告(多文件聚合)。"""

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
