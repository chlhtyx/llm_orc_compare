<script setup lang="ts">
import { RouterLink, RouterView, useRoute, useRouter } from 'vue-router'
import { computed, onMounted, ref } from 'vue'
import { getAppVersion } from '@/api/config'
import { logoutConsole } from '@/api/auth'
import { useAuthStore } from '@/stores/auth'

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()

// 版本号来自后端 __version__,接口失败时静默不显示(装饰信息,不阻塞主页面)。
const version = ref<string>('')
onMounted(async () => {
  try {
    const data = await getAppVersion()
    if (data?.version) version.value = data.version
  } catch {
    /* 忽略:版本号缺失不影响功能 */
  }
})
// 口令登录状态由路由守卫每次跳转刷新;这里只负责展示与退出。
const showLogout = computed(() => auth.auth_required && auth.authenticated)
async function logout() {
  try {
    await logoutConsole()
  } catch {
    /* 忽略:Cookie 未清也能重新登录 */
  }
  auth.authenticated = false
  router.replace({ name: 'login' })
}
const activeName = computed(() => {
  const name = String(route.name ?? '')
  if (name === 'report' || name === 'raw-report' || name === 'statement-report') return name
  if (name === 'settings') return 'settings'
  if (name === 'external-api-config') return 'external-api-config'
  if (name === 'history') return 'history'
  if (name === 'raw-submit') return 'raw-submit'
  if (name === 'statement-submit') return 'statement-submit'
  return 'submit'
})
// 「报告」链接按当前流程指向对应报告页。
const reportHref = computed(() => {
  const tid = route.params.taskId
  if (!tid) return null
  const name = String(route.name)
  if (name.startsWith('raw')) return `/raw/report/${tid}`
  if (name.startsWith('statement')) return `/statement/report/${tid}`
  return `/report/${tid}`
})
</script>

<template>
  <div class="app-shell">
    <header class="app-bar">
      <div class="app-brand">
        <span class="app-logo">DC</span>
        <span class="app-title">文档比对系统</span>
      </div>
      <nav class="app-nav">
        <RouterLink to="/" :class="{ active: activeName === 'submit' }">合同比对 API</RouterLink>
        <RouterLink to="/statement" :class="{ active: activeName === 'statement-submit' }">金额统计 API</RouterLink>
        <RouterLink
          v-if="reportHref"
          :to="reportHref"
          :class="{ active: activeName === 'report' || activeName === 'raw-report' || activeName === 'statement-report' }"
        >
          报告
        </RouterLink>
        <RouterLink to="/history" :class="{ active: activeName === 'history' }">
          对比记录
        </RouterLink>
        <RouterLink to="/api-config" :class="{ active: activeName === 'external-api-config' }">
          外部 API 配置
        </RouterLink>
        <RouterLink to="/settings" :class="{ active: activeName === 'settings' }">
          设置
        </RouterLink>
        <a v-if="showLogout" href="#" class="app-logout" @click.prevent="logout">退出</a>
      </nav>
    </header>
    <main class="app-main">
      <RouterView />
    </main>
    <footer class="app-foot">
      <span class="app-foot-left">Document Comparison · LLM OCR</span>
      <span v-if="version" class="app-version">v{{ version }}</span>
    </footer>
  </div>
</template>

<style scoped>
.app-foot {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.app-version {
  font-size: 12px;
}
.app-logout {
  color: var(--text-muted);
  margin-left: 4px;
}
</style>
