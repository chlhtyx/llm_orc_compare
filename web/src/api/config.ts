// LLM 模型配置读写,对齐后端 GET/PUT /api/v1/config/llm。
import { request } from './client'

/** 后端返回的生效配置(GET)。api_key 已脱敏。 */
export interface LlmConfig {
  // —— llm 引擎(通用 VL 模型)——
  llm_api_base: string
  llm_api_key: string // 脱敏,如 ****5678
  llm_api_key_set: boolean
  llm_model: string
  llm_timeout: number
  llm_max_concurrency: number
  llm_max_retries: number
  // —— paddleocr 引擎(专用 OCR 模型,独立配置)——
  paddleocr_api_mode: 'vllm' | 'official_sdk'
  paddleocr_api_base: string
  paddleocr_api_key: string // 脱敏
  paddleocr_api_key_set: boolean
  paddleocr_model: string
  paddleocr_official_api_base: string
  paddleocr_official_access_token: string // 脱敏
  paddleocr_official_access_token_set: boolean
  paddleocr_official_model: string
  paddleocr_timeout: number
  paddleocr_max_concurrency: number
  paddleocr_max_retries: number
  // —— LLM 辅助说明服务(纯文本 LLM)——
  judge_api_base: string
  judge_api_key: string // 脱敏
  judge_api_key_set: boolean
  judge_model: string
  judge_timeout: number
  // —— 语义向量引擎 ——
  embed_backend: string // mock | qwen | bge
  embed_api_base: string
  embed_api_key: string // 脱敏
  embed_api_key_set: boolean
  embed_model: string
  embed_timeout: number
  /** PDF 渲染 DPI(OCR 用),200 为速度/质量甜点 */
  pdf_render_dpi: number
  /** PDF 页数上限(0 表示不限制);提交超过此页数的扫描件会被直接拒绝 */
  max_pdf_pages: number
  // —— 外部系统 API ——
  external_api_key: string // 脱敏
  external_api_key_set: boolean
  external_public_base_url: string
  external_max_upload_mb: number
  external_image_dpi: number
  external_ocr_backend: 'llm' | 'paddleocr'
  external_enable_llm_judge: boolean
  external_enable_risk_assessment: boolean
  /** 回收件页数截取:回收 PDF 超过原始合同页数时,自动截取前 N 页再比对 */
  external_truncate_to_original_pages: boolean
  external_enabled: boolean
  persisted?: Record<string, unknown>
}

/** 更新请求(PUT)。全部可选;不传的字段不修改。 */
export interface LlmConfigUpdate {
  // —— llm 引擎 ——
  llm_api_base?: string
  /** 传空串清除已保存 key;传 "********" 表示不修改 */
  llm_api_key?: string
  llm_model?: string
  llm_timeout?: number
  llm_max_concurrency?: number
  // —— paddleocr 引擎 ——
  paddleocr_api_mode?: 'vllm' | 'official_sdk'
  paddleocr_api_base?: string
  paddleocr_api_key?: string
  paddleocr_model?: string
  paddleocr_official_api_base?: string
  paddleocr_official_access_token?: string
  paddleocr_official_model?: string
  paddleocr_timeout?: number
  paddleocr_max_concurrency?: number
  // —— LLM 辅助说明服务 ——
  judge_api_base?: string
  judge_api_key?: string
  judge_model?: string
  judge_timeout?: number
  // —— 语义向量引擎 ——
  embed_backend?: string
  embed_api_base?: string
  embed_api_key?: string
  embed_model?: string
  embed_timeout?: number
  pdf_render_dpi?: number
  max_pdf_pages?: number
  external_api_key?: string
  external_public_base_url?: string
  external_max_upload_mb?: number
  external_image_dpi?: number
  external_ocr_backend?: 'llm' | 'paddleocr'
  external_enable_llm_judge?: boolean
  external_enable_risk_assessment?: boolean
  external_truncate_to_original_pages?: boolean
}

export function getLlmConfig(): Promise<LlmConfig> {
  return request('/api/v1/config/llm')
}

export function updateLlmConfig(body: LlmConfigUpdate): Promise<{ status: string; config: LlmConfig }> {
  return request('/api/v1/config/llm', { method: 'PUT', body: JSON.stringify(body) })
}
