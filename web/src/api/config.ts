// LLM 模型配置读写,对齐后端 GET/PUT /api/v1/config/llm。
import { request } from './client'

/** 后端返回的生效配置(GET)。api_key 已脱敏。 */
export interface LlmConfig {
  llm_api_base: string
  llm_api_key: string // 脱敏,如 ****5678
  llm_api_key_set: boolean
  llm_model: string
  llm_timeout: number
  llm_max_concurrency: number
  // —— 语义向量引擎 ——
  embed_backend: string // mock | qwen | bge
  embed_api_base: string
  embed_api_key: string // 脱敏
  embed_api_key_set: boolean
  embed_model: string
  embed_timeout: number
  persisted: Record<string, unknown>
  config_file?: string
}

/** 更新请求(PUT)。全部可选;不传的字段不修改。 */
export interface LlmConfigUpdate {
  llm_api_base?: string
  /** 传空串清除已保存 key;传 "********" 表示不修改 */
  llm_api_key?: string
  llm_model?: string
  llm_timeout?: number
  llm_max_concurrency?: number
  // —— 语义向量引擎 ——
  embed_backend?: string
  embed_api_base?: string
  embed_api_key?: string
  embed_model?: string
  embed_timeout?: number
}

export function getLlmConfig(): Promise<LlmConfig> {
  return request('/api/v1/config/llm')
}

export function updateLlmConfig(body: LlmConfigUpdate): Promise<{ status: string; config: LlmConfig }> {
  return request('/api/v1/config/llm', { method: 'PUT', body: JSON.stringify(body) })
}
