// 与后端 src/document_comparison/models.py 的 pydantic 模型一一对应。
// 后端字段命名若变更,需同步此处。

export type DiffStatus = 'identical' | 'modified' | 'added' | 'deleted'
export type RiskLevel = 'high' | 'medium' | 'low' | 'none'
export type OverallRisk = 'high' | 'medium' | 'low' | 'changed' | 'clean' | 'needs_review'
export type RecognitionStatus = 'reliable' | 'needs_review'
export type ChangeVerdict = 'clean' | 'changed' | 'needs_review'
export type EvidenceConfidence = 'high' | 'medium' | 'low'
export type KeyElementKind =
  | 'amount'
  | 'date'
  | 'ratio'
  | 'term'
  | 'breach'
  | 'jurisdiction'
  | 'effective'
  | 'seal'
  | 'party'
  | 'account'
  | 'identifier'
  | 'negation'
export type BBoxShape = 'rect' | 'quad' | 'poly'
export type RegionKind = 'real' | 'placeholder'
export type TaskStatus = 'pending' | 'running' | 'done' | 'failed'

export interface DiffSegment {
  op: 'equal' | 'delete' | 'insert'
  text: string
}

export interface PageRegion {
  page_index: number
  bbox: number[] // 归一化 [x1,y1,x2,y2]
  shape: BBoxShape
  polygon?: number[][] | null
  kind?: RegionKind // real=真实高亮;placeholder=推断占位框(deleted 在回收件无对应内容,位置为推断)
}

export interface Diff {
  alignment_id: string
  status: DiffStatus
  segments: DiffSegment[]
  risk_level: RiskLevel
  risk_reasons: string[]
  verdict: ChangeVerdict
  confidence: EvidenceConfidence
  judged_by: 'rule' | 'llm'
  page_regions: PageRegion[]
  /** 原件 PDF 或 DOCX 派生 PDF 中经真实文本块验证的高亮区域。 */
  source_page_regions: PageRegion[]
  alignment_reason: string
  number: string
  title: string
}

export interface KeyElement {
  kind: KeyElementKind
  word_value: string
  pdf_value: string
  changed: boolean
  row_index: number
  col_header: string
}

export interface PageMeta {
  page_index: number
  width_px: number
  height_px: number
  pdf_width_pt: number
  pdf_height_pt: number
}

export interface PageRecognitionDiagnostic {
  page_index: number
  source: 'native' | 'fallback'
  reliable: boolean
  reasons: string[]
  char_count: number
  table_count: number
  location_status: 'complete' | 'partial' | 'missing'
  bbox_coverage: number
}

/** 回收件页数截取记录(等保审计留痕);仅在发生截断时存在。 */
export interface TruncationRecord {
  /** 截断前回收 PDF 实际页数 */
  original_pdf_page_count: number
  /** 截断后页数(=原始合同页数) */
  truncated_pdf_page_count: number
  /** 原始合同页数(估算或外部显式传入) */
  original_doc_page_count: number | null
  /** 原始合同页数来源:estimated=OOXML 估算,explicit=外部显式传入 */
  doc_page_count_source: 'estimated' | 'explicit' | null
  truncation_reason: 'original_page_count' | 'manual_target_body_end_page' | 'auto_trailing_drawings'
  excluded_page_numbers: number[]
  detection_confidence: number | null
  page_decisions: Array<{
    page_number: number
    page_type: 'contract_body' | 'engineering_drawing' | 'unknown'
    confidence: number
    signals: string[]
  }>
}

export interface TamperReport {
  source: string
  target: string
  overall_risk: OverallRisk
  change_status: ChangeVerdict
  summary: Record<string, unknown>
  diffs: Diff[]
  key_elements: KeyElement[]
  unmatched_clauses: Diff[]
  page_meta: PageMeta[]
  source_page_meta: PageMeta[]
  source_annotation_status: 'available' | 'partial' | 'unavailable'
  source_annotation_reason: string
  /** 回收件页数截取记录;null/undefined 表示未发生截断 */
  truncation: TruncationRecord | null
  recognition_status: RecognitionStatus
  recognition_diagnostics: PageRecognitionDiagnostic[]
  location_status: 'complete' | 'partial' | 'missing'
}

export interface CompareOptions {
  enable_llm_judge?: boolean
  /** 仅对歧义候选和 1↔N 拆并关系使用纯文本 LLM 辅助选择 */
  enable_llm_alignment?: boolean
  /**
   * 是否启用风险判别(高风险要素抽取 + 严重度分级 + LLM 辅助说明)。
   * 默认 false:仅列举字符级/表格级差异,不做风险判定。
   * true 时恢复完整风险分级行为。
   */
  enable_risk_assessment?: boolean
  /**
   * LLM 直接比对:开启后跳过条款切分/对齐,把 Word 与 PDF 解析成纯文本后
   * 直接交给 LLM 比对差异并标注。结果适配为标准报告;不做风险分级、无 PDF 高亮框。
   */
  enable_llm_direct_diff?: boolean
  /** OCR 引擎选择(由对比页每次提交时选择);不传则用默认 llm */
  ocr_backend?: 'llm' | 'paddleocr'
  /**
   * 回收件页数截取:开启后,回收 PDF 页数超过原始合同时,截取到原始页数再比对。
   * 用物理截断(生成前 N 页子集 PDF)保证 OCR/报告/高亮图在页数维度一致。
   */
  truncate_to_original_pages?: boolean
  /** 自动仅排除供应商 PDF 连续尾部的高置信度工程图 */
  auto_discard_trailing_drawings?: boolean
  /** 供应商 PDF 正文截止页(1 基);优先于自动识别和原件页数截取 */
  target_body_end_page?: number
  /** 原始合同页数(可选显式覆盖);不传时按 docx OOXML 分页符估算 */
  original_page_count?: number
}

export interface TaskInfo {
  task_id: string
  status: TaskStatus
  stage: string
  progress: number
  stage_timings: Record<string, number>
  overall_risk: OverallRisk | null
  error: string | null
  elapsed: number | null
  /** 仅 status === 'done' 时由 API 附加 */
  report?: TamperReport
}

export interface SubmitResponse {
  task_id: string
  status: TaskStatus
}

/** SSE 进度事件(后端 task_manager 产出)。字段为并集,按 type 判读。 */
export interface ProgressEvent {
  type?: string
  status?: TaskStatus
  stage?: string
  progress?: number
  stage_timings?: Record<string, number>
  overall_risk?: OverallRisk | null
  error?: string | null
  [key: string]: unknown
}

// —— 无标注版(纯文本 difflib)数据结构,与后端 raw_pipeline 对应 ——
export interface TextDiffHunk {
  tag: 'replace' | 'delete' | 'insert'
  word_lines: string[]
  pdf_lines: string[]
  char_segments: DiffSegment[]
  context_before: string[]
  context_after: string[]
}

export interface TextDiffReport {
  source: string
  target: string
  word_text: string
  pdf_text: string
  hunks: TextDiffHunk[]
  stats: Record<string, unknown>
  recognition_status: RecognitionStatus
  recognition_diagnostics: PageRecognitionDiagnostic[]
}

export interface RawCompareOptions {
  char_level?: boolean
  /** OCR 引擎选择(由对比页每次提交时选择);不传则用默认 llm */
  ocr_backend?: 'llm' | 'paddleocr'
}

/** 无标注版任务信息(GET /api/v1/raw-compare/{id}),done 时附加 raw_report。 */
export interface RawTaskInfo extends TaskInfo {
  raw_report?: TextDiffReport
}

// —— 金额统计(与后端 statement_pipeline 对应,独立通道)——
export type StatementColumnSource = 'heuristic' | 'llm' | 'none'

export interface StatementAmountItem {
  file_index: number
  file_name: string
  table_index: number
  page_index: number
  row_index: number
  row_label: string
  column: string
  raw_cell: string
  canonical: string
  value: number
}

export interface StatementTableSummary {
  file_index: number
  file_name: string
  table_index: number
  page_index: number
  headers: string[]
  column_sums: Record<string, number>
  declared_totals: Record<string, number>
  totals_match: Record<string, boolean>
  items: StatementAmountItem[]
  skipped_rows: number[]
  column_source: Record<string, StatementColumnSource>
  /** 本表含税金额合计(代码确定性算术) */
  tax_inclusive_total: number
  /** 含税口径来源,如「含税/价税合计列」「金额(不含税)列 + 税额列」「金额列」「LLM 抽取」 */
  tax_inclusive_method: string
}

export interface StatementFileSummary {
  file_index: number
  file_name: string
  total_amount: number
  tables: StatementTableSummary[]
  recognition_status: RecognitionStatus
  recognition_diagnostics: PageRecognitionDiagnostic[]
  error: string | null
}

export interface StatementSummaryReport {
  files: StatementFileSummary[]
  grand_total: number
  grand_totals_by_column: Record<string, number>
  total_files: number
  total_tables: number
  total_items: number
  verdict: ChangeVerdict
  reasons: string[]
  column_detection_summary: Record<string, number>
}

export interface StatementOptions {
  ocr_backend?: 'llm' | 'paddleocr'
  amount_column_keywords?: string[]
  enable_llm_column_detection?: boolean
}

/** 对帐单任务信息(GET /api/v1/statement/{id}),done 时附加 statement_report。 */
export interface StatementTaskInfo extends TaskInfo {
  statement_report?: StatementSummaryReport
}
