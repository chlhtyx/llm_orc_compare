// 每日调用统计端点封装,对齐 src/document_comparison/api/app.py 的 /api/v1/stats/daily
// 与 repository.daily_stats 的返回结构(按北京时间切天,缺失日期补零,升序)。
import { request } from './client'

/** 单日任务计数(按状态 / 类型细分)。 */
export interface DailyTaskStats {
  total: number
  by_status: Record<string, number>
  by_kind: Record<string, number>
}

/** 单日模型 / OCR 调用计数(成功 = error 为空且 status_code 为 2xx/3xx)。 */
export interface DailyCallStats {
  total: number
  success: number
  failed: number
  by_kind: Record<string, number>
}

/** 单日外部接口调用计数(成功 = status_code < 400)。 */
export interface DailyExternalStats {
  total: number
  success: number
  failed: number
  by_endpoint: Record<string, number>
}

export interface DailyStatsPoint {
  /** 北京时间日期,YYYY-MM-DD */
  date: string
  tasks: DailyTaskStats
  llm_calls: DailyCallStats
  external_calls: DailyExternalStats
}

export interface DailyStatsResponse {
  days: number
  timezone: string
  start: string
  end: string
  series: DailyStatsPoint[]
}

export interface DailyStatsParams {
  /** 仅未提供 start/end 时生效:最近 N 天(含今天)。 */
  days?: number
  /** 起始日期 YYYY-MM-DD(北京时间);提供后进入自由区间模式。 */
  start?: string
  /** 结束日期 YYYY-MM-DD(北京时间);缺省为今天,晚于今天由后端截断。 */
  end?: string
}

export function getDailyStats(params: DailyStatsParams = {}): Promise<DailyStatsResponse> {
  const qs = new URLSearchParams()
  if (params.days != null) qs.set('days', String(params.days))
  if (params.start) qs.set('start', params.start)
  if (params.end) qs.set('end', params.end)
  const query = qs.toString()
  return request(query ? `/api/v1/stats/daily?${query}` : '/api/v1/stats/daily')
}
