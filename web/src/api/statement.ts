// 对帐单金额统计端点封装,对齐 src/document_comparison/api/app.py 的 /api/v1/statement。
// 与 api/raw.ts、api/compare.ts 完全隔离,不共享 task store。
// 一次可上传多个 PDF,后端串行处理并聚合输出所有文件的总金额。
import type {
  ProgressEvent,
  StatementOptions,
  StatementSummaryReport,
  StatementTaskInfo,
  TaskStatus,
} from './types'
import { ApiError, openEventStream, request } from './client'

export { ApiError }

export interface StatementSubmitArgs {
  /** 多个 PDF 文件(至少 1 个) */
  targets: File[]
  options?: StatementOptions
  callbackUrl?: string
}

export function submitStatement(
  args: StatementSubmitArgs,
): Promise<{ task_id: string; status: TaskStatus; file_count: number }> {
  const form = new FormData()
  // 同字段名多次 append → 后端 list[UploadFile] 接收
  args.targets.forEach((f) => form.append('target', f))
  if (args.options) form.append('options', JSON.stringify(args.options))
  if (args.callbackUrl) form.append('callback_url', args.callbackUrl)
  return request('/api/v1/statement', { method: 'POST', body: form })
}

export function getStatementTask(taskId: string): Promise<StatementTaskInfo> {
  return request(`/api/v1/statement/${encodeURIComponent(taskId)}`)
}

export function getStatementReport(taskId: string): Promise<StatementSummaryReport> {
  return request(`/api/v1/statement/${encodeURIComponent(taskId)}/report`)
}

export interface StatementProgressHandlers {
  onEvent: (ev: ProgressEvent) => void
  onDone?: (status: TaskStatus) => void
  onError?: (err: Error) => void
}

export function subscribeStatementProgress(
  taskId: string,
  handlers: StatementProgressHandlers,
): AbortController {
  return openEventStream(
    `/api/v1/statement/${encodeURIComponent(taskId)}/events`,
    handlers,
  )
}
