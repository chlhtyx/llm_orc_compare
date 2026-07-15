// 与后端 src/document_comparison/models.py 的 pydantic 模型一一对应。
// 后端字段命名若变更,需同步此处。

export type DocType = 'word' | 'pdf'
export type MatchType = 'number' | 'field' | 'normalized_exact' | 'semantic' | 'unmatched'
export type DiffStatus = 'identical' | 'modified' | 'added' | 'deleted'
export type RiskLevel = 'high' | 'medium' | 'low' | 'none'
export type OverallRisk = 'high' | 'medium' | 'low' | 'clean' | 'needs_review'
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
  recognition_status: RecognitionStatus
  recognition_diagnostics: PageRecognitionDiagnostic[]
}

export interface CompareOptions {
  enable_llm_judge?: boolean
}

export interface TaskInfo {
  task_id: string
  status: TaskStatus
  stage: string
  progress: number
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
}

/** 无标注版任务信息(GET /api/v1/raw-compare/{id}),done 时附加 raw_report。 */
export interface RawTaskInfo extends TaskInfo {
  raw_report?: TextDiffReport
}
