// 控制台登录状态。每次路由跳转由守卫重新拉 /api/v1/auth/status:
// 会话过期后守卫立即生效,无需全局失效广播;未启用口令时两标志恒为 false/true。
import { defineStore } from 'pinia'
import { getAuthStatus } from '@/api/auth'

export const useAuthStore = defineStore('consoleAuth', {
  state: () => ({
    auth_required: false,
    authenticated: false,
    loaded: false,
  }),
  actions: {
    async refresh() {
      const data = await getAuthStatus()
      this.auth_required = data.auth_required
      this.authenticated = data.authenticated
      this.loaded = true
    },
  },
})
