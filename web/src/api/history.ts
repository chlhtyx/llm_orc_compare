// 比对记录历史端点封装,对齐 src/document_comparison/api/app.py 的 /api/v1/tasks。
// 与 api/compare.ts / raw.ts / statement.ts 平级,不依赖具体报告 store。
import type { OverallRisk, TaskStatus } from './types'
import { ApiError, request } from './client'

export { ApiError }

export type TaskKind = 'compare' | 'raw' | 'statement'

/** 列表项(后端 repository.to_dict,不含报告 JSONB)。 */
export interface TaskListItem {
  task_id: string
  kind: TaskKind
  status: TaskStatus
  source_name: string
  target_names: string[]
  ocr_backend: string | null
  /** 单据号(仅外部接口核对请求携带,普通提交为 null)。 */
  document_no: string | null
  /** 是否外部接口提交的任务(携带 document_no)。 */
  external_request: boolean
  overall_risk: OverallRisk | null
  change_status: 'clean' | 'changed' | 'needs_review' | null
  elapsed: number | null
  stage_timings: Record<string, number>
  error: string | null
  created_at: string | null // ISO
  finished_at: string | null // ISO
}

/** 单任务里程碑事件(后端 repository.event_to_dict)。 */
export interface TaskEventItem {
  id: number
  stage: string
  progress: number
  stage_timings: Record<string, number>
  milestone: boolean
  created_at: string | null
}

/** 对话型 LLM 调用 kind(后端 observability._COLLECTED_KINDS)。embedding 不入。 */
export type LlmCallKind =
  | 'ocr'
  | 'ocr-whole'
  | 'paddleocr'
  | 'judge'
  | 'llm-diff'
  | 'statement-column'

/** 单次 LLM 调用记录(后端 repository.llm_call_to_dict)。 */
export interface LlmCallItem {
  id: number
  task_id: string
  kind: LlmCallKind | string   // 容忍后端未来新增 kind 不崩前端
  attempt: number
  status_code: number | null   // null = 连接级失败(超时/网络)
  elapsed_ms: number | null
  error: string | null
  /** payload 中图片 base64 已被脱敏为 {data_url, base64_chars, sha256} */
  payload: Record<string, unknown>
  /** response 截断到 64KB;失败为 null */
  response: Record<string, unknown> | null
  created_at: string | null    // ISO
}

export interface ListTasksParams {
  kind?: TaskKind
  status?: TaskStatus
  /** 模糊搜索关键字,后端匹配 task_id/document_no/source_name/target_names。 */
  q?: string
  limit?: number
  offset?: number
}

export interface ListTasksResponse {
  items: TaskListItem[]
  total: number
}

export function listTasks(params: ListTasksParams = {}): Promise<ListTasksResponse> {
  const qs = new URLSearchParams()
  if (params.kind) qs.set('kind', params.kind)
  if (params.status) qs.set('status', params.status)
  // trim 后非空才作为搜索条件,空串等同未搜索
  const q = params.q?.trim()
  if (q) qs.set('q', q)
  if (params.limit != null) qs.set('limit', String(params.limit))
  if (params.offset != null) qs.set('offset', String(params.offset))
  const query = qs.toString()
  return request(query ? `/api/v1/tasks?${query}` : '/api/v1/tasks')
}

export function getTaskEvents(taskId: string): Promise<{ task_id: string; items: TaskEventItem[] }> {
  return request(`/api/v1/tasks/${encodeURIComponent(taskId)}/events`)
}

export function getTaskLlmCalls(taskId: string): Promise<{ task_id: string; items: LlmCallItem[] }> {
  return request(`/api/v1/tasks/${encodeURIComponent(taskId)}/llm-calls`)
}

/** 按 kind 推导该任务对应的「查看报告」路由路径。 */
export function reportRouteFor(item: Pick<TaskListItem, 'task_id' | 'kind'>): string {
  switch (item.kind) {
    case 'raw':
      return `/raw/report/${item.task_id}`
    case 'statement':
      return `/statement/report/${item.task_id}`
    case 'compare':
    default:
      return `/report/${item.task_id}`
  }
}
