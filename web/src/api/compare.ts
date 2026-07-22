// 比对相关端点封装,对齐 src/document_comparison/api/app.py。
import type {
  CompareOptions,
  ProgressEvent,
  SubmitResponse,
  TaskInfo,
  TaskStatus,
  TamperReport,
} from './types'
import { ApiError, apiUrl, openEventStream, request } from './client'

export function health(): Promise<{ status: string }> {
  return request('/health')
}

export interface SubmitArgs {
  source: File // .docx
  target: File // .pdf
  options?: CompareOptions
  callbackUrl?: string
  callbackSecret?: string
}

export function submitCompare(args: SubmitArgs): Promise<SubmitResponse> {
  const form = new FormData()
  form.append('source', args.source)
  form.append('target', args.target)
  if (args.options) form.append('options', JSON.stringify(args.options))
  if (args.callbackUrl) form.append('callback_url', args.callbackUrl)
  if (args.callbackSecret) form.append('callback_secret', args.callbackSecret)
  return request('/api/v1/compare', { method: 'POST', body: form })
}

export interface CompareApiTestImage {
  page_number: number
  has_highlight: boolean
  url: string
}

export interface CompareApiTestInfo {
  task_id: string
  document_no: string
  status: 'pending' | 'running' | 'done' | 'failed'
  stage?: string
  progress?: number
  error?: string | null
  result?: {
    change_status: 'clean' | 'changed' | 'needs_review'
    recognition_status: 'reliable' | 'needs_review'
    location_status: 'complete' | 'partial' | 'missing'
    summary: Record<string, unknown>
    result_text: string
  }
  highlight_images?: CompareApiTestImage[]
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

export function getReport(taskId: string, format: 'json' = 'json'): Promise<TamperReport> {
  return request(
    `/api/v1/compare/${encodeURIComponent(taskId)}/report?format=${format}`,
  )
}

/** 返回 PDF 源文件的可访问 URL，供 <iframe> 或 pdf.js 加载。 */
export function getSourcePdfUrl(taskId: string): string {
  return apiUrl(`/api/v1/compare/${encodeURIComponent(taskId)}/source`)
}

/** 返回带差异高亮的 Word 报告下载 URL（整段黄底标注被篡改条款）。 */
export function getAnnotatedDocxUrl(taskId: string): string {
  return apiUrl(
    `/api/v1/compare/${encodeURIComponent(taskId)}/report?format=docx`,
  )
}

/** 返回带差异高亮框的 PDF 报告下载 URL（扫描件需 OCR/Spotting 提供坐标）。 */
export function getAnnotatedPdfUrl(taskId: string): string {
  return apiUrl(
    `/api/v1/compare/${encodeURIComponent(taskId)}/report?format=pdf`,
  )
}

/** docx 高亮预览段落（后端 /docx-preview 接口返回）。 */
export interface PreviewParagraph {
  text: string
  highlight: '' | 'modified' | 'deleted'
  status: string
  risk_level: string
  number: string
}

/** 拉取源 docx 的全段落 + 差异标记，供 HTML 渲染在线高亮预览。 */
export async function getDocxPreview(taskId: string): Promise<PreviewParagraph[]> {
  const data = await request<{ paragraphs: PreviewParagraph[] }>(
    `/api/v1/compare/${encodeURIComponent(taskId)}/docx-preview`,
  )
  return data.paragraphs
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
