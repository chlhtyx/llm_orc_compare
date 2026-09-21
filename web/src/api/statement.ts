// 金额统计端点封装,对齐 src/document_comparison/api/app.py 的 /api/v1/statement。
// 与 api/raw.ts、api/compare.ts 完全隔离,不共享 task store。
// 一次可上传多个 PDF,后端串行处理并聚合输出所有文件的总金额。
import type {
  ProgressEvent,
  StatementOptions,
  StatementTaskInfo,
  TaskStatus,
} from './types'
import { ApiError, apiUrl, openEventStream, request } from './client'

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

/** 下载报告中指定文件的原始 PDF，fileIndex 为零基序号。 */
export function statementAttachmentUrl(taskId: string, fileIndex: number): string {
  return apiUrl(`/api/v1/statement/${encodeURIComponent(taskId)}/files/${fileIndex}/download`)
}

/** 金额统计 API 管线测试的对外任务状态（不发送真实回调）。 */
export interface StatementApiTestInfo {
  task_id: string
  document_no: string
  status: TaskStatus
  stage?: string
  progress?: number
  error?: string | null
  grand_total?: number
  verdict?: 'clean' | 'changed' | 'needs_review'
  file_totals?: Array<{
    file_index: number
    file_name: string
    total_amount: number
    error: string | null
  }>
  total_files?: number
  total_tables?: number
  total_items?: number
  reasons?: string[]
  result_url?: string
}

export interface StatementApiTestSubmitArgs {
  targets: File[]
  targetUrls: string[]
}

/** 使用金额统计页已选文件或 URL 验证正式外部 API 的完整统计管线，不发送回调。 */
export function submitStatementApiTest(args: StatementApiTestSubmitArgs): Promise<StatementApiTestInfo> {
  const form = new FormData()
  args.targets.forEach((file) => form.append('target', file))
  if (args.targetUrls.length > 0) form.append('target_urls', JSON.stringify(args.targetUrls))
  return request('/api/v1/statement/api-test', { method: 'POST', body: form })
}

export function getStatementApiTest(taskId: string): Promise<StatementApiTestInfo> {
  return request(`/api/v1/statement/api-test/${encodeURIComponent(taskId)}`)
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
