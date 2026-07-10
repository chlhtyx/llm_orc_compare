// LLM 模型配置状态:从后端读取生效配置,提供保存能力。
import { defineStore } from 'pinia'
import { ref } from 'vue'
import { getLlmConfig, updateLlmConfig, type LlmConfig, type LlmConfigUpdate } from '@/api/config'
import { ApiError } from '@/api/compare'

export const useModelConfigStore = defineStore('modelConfig', () => {
  const config = ref<LlmConfig | null>(null)
  const loading = ref(false)
  const saving = ref(false)
  const error = ref<string | null>(null)

  async function fetch(): Promise<void> {
    loading.value = true
    error.value = null
    try {
      config.value = await getLlmConfig()
    } catch (e) {
      error.value = e instanceof ApiError ? e.message : `加载失败: ${(e as Error).message}`
    } finally {
      loading.value = false
    }
  }

  async function save(update: LlmConfigUpdate): Promise<boolean> {
    saving.value = true
    error.value = null
    try {
      const res = await updateLlmConfig(update)
      config.value = { ...config.value, ...res.config } as LlmConfig
      return true
    } catch (e) {
      error.value = e instanceof ApiError ? e.message : `保存失败: ${(e as Error).message}`
      return false
    } finally {
      saving.value = false
    }
  }

  return { config, loading, saving, error, fetch, save }
})
