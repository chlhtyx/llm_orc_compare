// 极简 HTTP 客户端:
// - 统一错误归一化为 ApiError {code, message, request_id}
// - 提供 fetch + ReadableStream 的 SSE 读取
export interface NormalizedError {
  code: number
  message: string
  request_id?: string
}

export class ApiError extends Error {
  code: number
  request_id?: string
  constructor(e: NormalizedError) {
    super(e.message)
    this.name = 'ApiError'
    this.code = e.code
    this.request_id = e.request_id
  }
}

/** 拼接后端基址。开发期 VITE_API_BASE 为空 → 同源,走 vite proxy。 */
export function apiUrl(path: string): string {
  const base = (import.meta.env.VITE_API_BASE ?? '').replace(/\/$/, '')
  return `${base}${path}`
}

interface RequestOptions {
  method?: 'GET' | 'POST' | 'PUT' | 'DELETE'
  body?: BodyInit
  /** true 时跳过 JSON 解析,返回 Response */
  raw?: boolean
  signal?: AbortSignal
}

async function request<T>(path: string, opts: RequestOptions = {}): Promise<T> {
  const headers = new Headers()
  if (opts.body instanceof FormData) {
    // FormData 不能手动设 Content-Type,浏览器自动带 boundary
  } else if (opts.body !== undefined) {
    headers.set('Content-Type', 'application/json')
  }

  let res: Response
  try {
    res = await fetch(apiUrl(path), {
      method: opts.method ?? 'GET',
      headers,
      body: opts.body,
      signal: opts.signal,
    })
  } catch (e) {
    throw new ApiError({
      code: 0,
      message: `网络请求失败: ${(e as Error).message}`,
    })
  }

  if (!res.ok) {
    throw new ApiError(await normalizeError(res))
  }

  if (opts.raw) return res as unknown as T

  const text = await res.text()
  return (text ? JSON.parse(text) : null) as T
}

async function normalizeError(res: Response): Promise<NormalizedError> {
  try {
    const data = await res.json()
    return {
      code: data.code ?? res.status,
      message: data.message ?? res.statusText,
      request_id: data.request_id,
    }
  } catch {
    return { code: res.status, message: res.statusText }
  }
}

/**
 * 读取 SSE 进度流(后端 GET /api/v1/compare/{id}/events)。
 * 解析 `data: {...}\n\n` 与 `event: done` 帧格式。
 */
export interface SSEHandlers {
  onEvent: (ev: import('./types').ProgressEvent) => void
  onDone?: (finalStatus: import('./types').TaskStatus) => void
  onError?: (err: Error) => void
}

export function openEventStream(path: string, handlers: SSEHandlers): AbortController {
  const controller = new AbortController()
  const headers = new Headers()
  headers.set('Accept', 'text/event-stream')

  ;(async () => {
    let res: Response
    try {
      res = await fetch(apiUrl(path), {
        method: 'GET',
        headers,
        signal: controller.signal,
      })
    } catch (e) {
      if (!controller.signal.aborted) handlers.onError?.(e as Error)
      return
    }
    if (!res.ok || !res.body) {
      handlers.onError?.(new ApiError(await normalizeError(res)))
      return
    }

    const reader = res.body.getReader()
    const decoder = new TextDecoder('utf-8')
    let buffer = ''

    try {
      for (;;) {
        const { value, done } = await reader.read()
        if (done) break
        buffer += decoder.decode(value, { stream: true })

        let sep: number
        while ((sep = buffer.indexOf('\n\n')) !== -1) {
          const frame = buffer.slice(0, sep)
          buffer = buffer.slice(sep + 2)
          parseSseFrame(frame, handlers)
        }
      }
      if (buffer.trim()) parseSseFrame(buffer, handlers)
    } catch (e) {
      if (!controller.signal.aborted) handlers.onError?.(e as Error)
    }
  })()

  return controller
}

function parseSseFrame(frame: string, handlers: SSEHandlers): void {
  let event = ''
  let data = ''
  for (const line of frame.split('\n')) {
    if (line.startsWith('event:')) event = line.slice(6).trim()
    else if (line.startsWith('data:')) data += line.slice(5).trim()
  }
  if (!data) return
  let payload: import('./types').ProgressEvent
  try {
    payload = JSON.parse(data)
  } catch {
    return
  }
  if (event === 'done') {
    handlers.onDone?.((payload.status as import('./types').TaskStatus) ?? 'done')
    return
  }
  handlers.onEvent(payload)
}

export { request }
