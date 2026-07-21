<script setup lang="ts">
import { RouterLink, RouterView, useRoute } from 'vue-router'
import { computed } from 'vue'

const route = useRoute()
const activeName = computed(() => {
  const name = String(route.name ?? '')
  if (name === 'report' || name === 'raw-report' || name === 'statement-report') return name
  if (name === 'settings') return 'settings'
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
        <RouterLink to="/" :class="{ active: activeName === 'submit' }">合同比对</RouterLink>
        <RouterLink to="/raw" :class="{ active: activeName === 'raw-submit' }">无标注比对</RouterLink>
        <RouterLink to="/statement" :class="{ active: activeName === 'statement-submit' }">对帐单统计</RouterLink>
        <RouterLink
          v-if="reportHref"
          :to="reportHref"
          :class="{ active: activeName === 'report' || activeName === 'raw-report' || activeName === 'statement-report' }"
        >
          报告
        </RouterLink>
        <RouterLink to="/history" :class="{ active: activeName === 'history' }">
          比对记录
        </RouterLink>
        <RouterLink to="/settings" :class="{ active: activeName === 'settings' }">
          设置
        </RouterLink>
      </nav>
    </header>
    <main class="app-main">
      <RouterView />
    </main>
    <footer class="app-foot">
      <span>Document Comparison · LLM OCR</span>
    </footer>
  </div>
</template>
