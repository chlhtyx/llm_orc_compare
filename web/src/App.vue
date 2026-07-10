<script setup lang="ts">
import { RouterLink, RouterView, useRoute } from 'vue-router'
import { computed } from 'vue'

const route = useRoute()
const activeName = computed(() =>
  route.name === 'report' ? 'report' : route.name === 'settings' ? 'settings' : 'submit',
)
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
        <RouterLink
          v-if="route.params.taskId"
          :to="`/report/${route.params.taskId}`"
          :class="{ active: activeName === 'report' }"
        >
          报告
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
      <span>Document Comparison · PaddleOCR-VL</span>
    </footer>
  </div>
</template>
