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

export interface ListTasksParams {
  kind?: TaskKind
  status?: TaskStatus
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
  if (params.limit != null) qs.set('limit', String(params.limit))
  if (params.offset != null) qs.set('offset', String(params.offset))
  const query = qs.toString()
  return request(query ? `/api/v1/tasks?${query}` : '/api/v1/tasks')
}

export function getTaskEvents(taskId: string): Promise<{ task_id: string; items: TaskEventItem[] }> {
  return request(`/api/v1/tasks/${encodeURIComponent(taskId)}/events`)
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
