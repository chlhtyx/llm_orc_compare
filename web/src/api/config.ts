// LLM 模型配置读写,对齐后端 GET/PUT /api/v1/config/llm。
import { request } from './client'

/** 对话型 LLM 接口协议:openai=Chat Completions(默认)、openai_responses=OpenAI 新版 Responses API、anthropic=Anthropic Messages */
export type LlmApiProtocol = 'openai' | 'openai_responses' | 'anthropic'

/** 后端返回的生效配置(GET)。api_key 已脱敏。 */
export interface LlmConfig {
  // —— llm 引擎(通用 VL 模型)——
  llm_api_protocol: LlmApiProtocol
  llm_api_base: string
  llm_api_key: string // 脱敏,如 ****5678
  llm_api_key_set: boolean
  llm_model: string
  llm_timeout: number
  llm_max_concurrency: number
  llm_max_retries: number
  // —— paddleocr 引擎(专用 OCR 模型,独立配置)——
  paddleocr_api_mode: 'vllm' | 'official_sdk' | 'paddlex_serving'
  paddleocr_api_base: string
  paddleocr_api_key: string // 脱敏
  paddleocr_api_key_set: boolean
  paddleocr_model: string
  paddleocr_official_api_base: string
  paddleocr_official_access_token: string // 脱敏
  paddleocr_official_access_token_set: boolean
  paddleocr_official_model: string
  paddleocr_paddlex_api_base: string
  paddleocr_paddlex_endpoint: string
  paddleocr_paddlex_api_key: string // 脱敏
  paddleocr_paddlex_api_key_set: boolean
  paddleocr_timeout: number
  paddleocr_max_concurrency: number
  paddleocr_max_retries: number
  // —— LLM 辅助说明服务(纯文本 LLM)——
  judge_api_protocol: LlmApiProtocol
  judge_api_base: string
  judge_api_key: string // 脱敏
  judge_api_key_set: boolean
  judge_model: string
  judge_timeout: number
  /** 合同 LLM 直接比对系统提示词;留空(空串)使用内置默认规则 */
  llm_direct_diff_prompt: string
  /** 只读:内置默认提示词全文(llm_direct_diff_prompt 为空时生效的前半段),供 UI 展示 */
  llm_direct_diff_default_prompt: string
  /**
   * /no_think 指令开关:True 时在比对调用系统提示词末尾追加 /no_think,
   * 关闭 GLM-4.5/4.6 的 <think> 思考链(与 Qwen3 的 enable_thinking=False 并存)。
   * 影响 LLM 直接比对与风险复核两条通道;非 GLM 模型自动忽略。默认 True。
   */
  llm_diff_no_think_enabled: boolean
  // —— 语义向量引擎 ——
  embed_backend: string // mock | qwen | bge
  embed_api_base: string
  embed_api_key: string // 脱敏
  embed_api_key_set: boolean
  embed_model: string
  embed_timeout: number
  /** PDF 渲染 DPI(OCR 用),200 为速度/质量甜点 */
  pdf_render_dpi: number
  /** 检测到红色印章后，对该页执行颜色抑制二次 OCR */
  seal_recovery_enabled: boolean
  /** 印章二次 OCR 的局部重渲染 DPI */
  seal_recovery_dpi: number
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
  external_enable_llm_alignment: boolean
  external_enable_risk_assessment: boolean
  /** LLM 直接比对:解析后直接交给 LLM 比差异;复用外部 API / 管线测试默认配置 */
  external_enable_llm_direct_diff: boolean
  /** 回收件页数截取:回收 PDF 超过原始合同页数时,自动截取前 N 页再比对 */
  external_truncate_to_original_pages: boolean
  external_enabled: boolean
  persisted?: Record<string, unknown>
}

/** 更新请求(PUT)。全部可选;不传的字段不修改。 */
export interface LlmConfigUpdate {
  // —— llm 引擎 ——
  llm_api_protocol?: LlmApiProtocol
  llm_api_base?: string
  /** 传空串清除已保存 key;传 "********" 表示不修改 */
  llm_api_key?: string
  llm_model?: string
  llm_timeout?: number
  llm_max_concurrency?: number
  // —— paddleocr 引擎 ——
  paddleocr_api_mode?: 'vllm' | 'official_sdk' | 'paddlex_serving'
  paddleocr_api_base?: string
  paddleocr_api_key?: string
  paddleocr_model?: string
  paddleocr_official_api_base?: string
  paddleocr_official_access_token?: string
  paddleocr_official_model?: string
  paddleocr_paddlex_api_base?: string
  paddleocr_paddlex_endpoint?: string
  paddleocr_paddlex_api_key?: string
  paddleocr_timeout?: number
  paddleocr_max_concurrency?: number
  // —— LLM 辅助说明服务 ——
  judge_api_protocol?: LlmApiProtocol
  judge_api_base?: string
  judge_api_key?: string
  judge_model?: string
  judge_timeout?: number
  /** 合同 LLM 直接比对系统提示词;传空串=回退内置默认(清除自定义) */
  llm_direct_diff_prompt?: string
  /** /no_think 指令开关(见 LlmConfig.llm_diff_no_think_enabled 说明) */
  llm_diff_no_think_enabled?: boolean
  // —— 语义向量引擎 ——
  embed_backend?: string
  embed_api_base?: string
  embed_api_key?: string
  embed_model?: string
  embed_timeout?: number
  pdf_render_dpi?: number
  seal_recovery_enabled?: boolean
  seal_recovery_dpi?: number
  max_pdf_pages?: number
  external_api_key?: string
  external_public_base_url?: string
  external_max_upload_mb?: number
  external_image_dpi?: number
  external_ocr_backend?: 'llm' | 'paddleocr'
  external_enable_llm_judge?: boolean
  external_enable_llm_alignment?: boolean
  external_enable_risk_assessment?: boolean
  external_enable_llm_direct_diff?: boolean
  external_truncate_to_original_pages?: boolean
}

export function getLlmConfig(): Promise<LlmConfig> {
  return request('/api/v1/config/llm')
}

/** 应用版本号(单一真相源:后端 document_comparison.__version__)。 */
export function getAppVersion(): Promise<{ version: string }> {
  return request('/api/v1/version')
}

export function updateLlmConfig(body: LlmConfigUpdate): Promise<{ status: string; config: LlmConfig }> {
  return request('/api/v1/config/llm', { method: 'PUT', body: JSON.stringify(body) })
}
