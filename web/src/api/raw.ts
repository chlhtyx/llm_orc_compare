// 无标注版(纯文本 difflib)端点封装,对齐 src/document_comparison/api/app.py 的 /api/v1/raw-compare。
// 与 api/compare.ts 完全隔离,不共享 task store。
import type {
  ProgressEvent,
  RawCompareOptions,
  RawTaskInfo,
  TaskStatus,
} from './types'
import { ApiError, openEventStream, request } from './client'

export { ApiError }

export interface RawSubmitArgs {
  source: File // .docx 或 .pdf
  target: File // .pdf
  options?: RawCompareOptions
  callbackUrl?: string
}

export function submitRawCompare(args: RawSubmitArgs): Promise<{ task_id: string; status: TaskStatus }> {
  const form = new FormData()
  form.append('source', args.source)
  form.append('target', args.target)
  if (args.options) form.append('options', JSON.stringify(args.options))
  if (args.callbackUrl) form.append('callback_url', args.callbackUrl)
  return request('/api/v1/raw-compare', { method: 'POST', body: form })
}

export function getRawTask(taskId: string): Promise<RawTaskInfo> {
  return request(`/api/v1/raw-compare/${encodeURIComponent(taskId)}`)
}


export interface RawProgressHandlers {
  onEvent: (ev: ProgressEvent) => void
  onDone?: (status: TaskStatus) => void
  onError?: (err: Error) => void
}

export function subscribeRawProgress(taskId: string, handlers: RawProgressHandlers): AbortController {
  return openEventStream(
    `/api/v1/raw-compare/${encodeURIComponent(taskId)}/events`,
    handlers,
  )
}
