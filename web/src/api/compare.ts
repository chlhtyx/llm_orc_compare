// 比对相关端点封装,对齐 src/document_comparison/api/app.py。
import type {
  CompareOptions,
  ProgressEvent,
  SubmitResponse,
  TaskInfo,
  TaskStatus,
} from './types'
import { ApiError, apiUrl, openEventStream, request } from './client'

export interface SubmitArgs {
  source: File // .docx 或 .pdf
  target: File // .pdf
  options?: CompareOptions
  callbackUrl?: string
}

export function submitCompare(args: SubmitArgs): Promise<SubmitResponse> {
  const form = new FormData()
  form.append('source', args.source)
  form.append('target', args.target)
  if (args.options) form.append('options', JSON.stringify(args.options))
  if (args.callbackUrl) form.append('callback_url', args.callbackUrl)
  return request('/api/v1/compare', { method: 'POST', body: form })
}

export interface CompareApiTestInfo {
  task_id: string
  document_no: string
  status: 'pending' | 'running' | 'done' | 'failed'
  stage?: string
  progress?: number
  error?: string | null
  change_status?: 'clean' | 'changed' | 'needs_review'
  result_text?: string
  highlight_images?: string[]
}

/** 使用合同比对页已选文件验证外部 API 的完整产物管线，不发送回调。 */
export function submitCompareApiTest(source: File, target: File): Promise<CompareApiTestInfo> {
  const form = new FormData()
  form.append('source', source)
  form.append('target', target)
  return request('/api/v1/compare/api-test', { method: 'POST', body: form })
}

export function getCompareApiTest(taskId: string): Promise<CompareApiTestInfo> {
  return request(`/api/v1/compare/api-test/${encodeURIComponent(taskId)}`)
}

export function getCompareApiTestImageUrl(taskId: string, pageNumber: number): string {
  return apiUrl(
    `/api/v1/compare/api-test/${encodeURIComponent(taskId)}/images/${pageNumber}`,
  )
}

export function getTask(taskId: string): Promise<TaskInfo> {
  return request(`/api/v1/compare/${encodeURIComponent(taskId)}`)
}

/** 返回 PDF 源文件的可访问 URL，供 <iframe> 或 pdf.js 加载。 */
export function getSourcePdfUrl(taskId: string): string {
  return apiUrl(`/api/v1/compare/${encodeURIComponent(taskId)}/source`)
}

/** 返回原件 PDF 的可访问 URL；DOCX 原件由服务端明确返回 409。 */
export function getOriginalPdfUrl(taskId: string): string {
  return apiUrl(`/api/v1/compare/${encodeURIComponent(taskId)}/original-pdf`)
}

/** 下载自包含 HTML 报告；包含差异表和可联动跳转的高亮页面。 */
export function getHtmlReportUrl(taskId: string): string {
  return apiUrl(`/api/v1/compare/${encodeURIComponent(taskId)}/report?format=html`)
}

export interface ProgressHandlers {
  onEvent: (ev: ProgressEvent) => void
  onDone?: (status: TaskStatus) => void
  onError?: (err: Error) => void
}

/** 订阅任务 SSE 进度。返回 AbortController,用于取消订阅。 */
export function subscribeProgress(taskId: string, handlers: ProgressHandlers): AbortController {
  return openEventStream(
    `/api/v1/compare/${encodeURIComponent(taskId)}/events`,
    handlers,
  )
}

export { ApiError }
