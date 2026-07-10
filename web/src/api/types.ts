// 与后端 src/document_comparison/models.py 的 pydantic 模型一一对应。
// 后端字段命名若变更,需同步此处。

export type DocType = 'word' | 'pdf'
export type MatchType = 'number' | 'semantic' | 'unmatched'
export type DiffStatus = 'identical' | 'modified' | 'added' | 'deleted'
export type RiskLevel = 'high' | 'medium' | 'low' | 'none'
export type OverallRisk = 'high' | 'medium' | 'low' | 'clean'
export type KeyElementKind =
  | 'amount'
  | 'date'
  | 'ratio'
  | 'term'
  | 'breach'
  | 'jurisdiction'
  | 'effective'
  | 'seal'
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
  page_regions: PageRegion[]
  number: string
  title: string
}

export interface KeyElement {
  kind: KeyElementKind
  word_value: string
  pdf_value: string
  changed: boolean
}

export interface PageMeta {
  page_index: number
  width_px: number
  height_px: number
  pdf_width_pt: number
  pdf_height_pt: number
}

export interface TamperReport {
  source: string
  target: string
  overall_risk: OverallRisk
  summary: Record<string, unknown>
  diffs: Diff[]
  key_elements: KeyElement[]
  unmatched_clauses: Diff[]
  page_meta: PageMeta[]
}

export interface CompareOptions {
  similarity_identical?: number
  similarity_modified?: number
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
