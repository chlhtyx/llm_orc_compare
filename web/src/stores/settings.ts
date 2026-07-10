// 全局设置:API Key(优先 localStorage,回落 VITE_API_KEY)。
// 提交页提供输入框,便于在界面内切换 key 而无需重建。
import { defineStore } from 'pinia'
import { computed, ref, watch } from 'vue'

const LS_KEY = 'dc.apiKey'

function readInitialKey(): string {
  try {
    const v = localStorage.getItem(LS_KEY)
    if (v && v.trim()) return v.trim()
  } catch {
    // 忽略隐私模式等读取失败
  }
  return import.meta.env.VITE_API_KEY ?? ''
}

export const useSettingsStore = defineStore('settings', () => {
  const apiKey = ref<string>(readInitialKey())

  watch(
    apiKey,
    (v) => {
      try {
        if (v && v.trim()) localStorage.setItem(LS_KEY, v.trim())
        else localStorage.removeItem(LS_KEY)
      } catch {
        // 忽略写入失败
      }
    },
    { flush: 'post' },
  )

  /** 客户端实际使用的 key:输入框为空时回落到 env。 */
  const resolvedApiKey = computed(() => apiKey.value.trim() || (import.meta.env.VITE_API_KEY ?? ''))

  return { apiKey, resolvedApiKey }
})
