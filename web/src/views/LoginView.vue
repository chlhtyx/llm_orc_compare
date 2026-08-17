<script setup lang="ts">
import { ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { loginConsole } from '@/api/auth'
import { useAuthStore } from '@/stores/auth'

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()

const password = ref('')
const error = ref('')
const submitting = ref(false)

async function submit() {
  if (!password.value || submitting.value) return
  submitting.value = true
  error.value = ''
  try {
    await loginConsole(password.value)
    auth.authenticated = true
    // 只回跳站内地址,避免开放重定向
    const redirect =
      typeof route.query.redirect === 'string' && route.query.redirect.startsWith('/')
        ? route.query.redirect
        : '/'
    router.replace(redirect)
  } catch (e) {
    error.value = (e as Error).message || '登录失败'
    password.value = ''
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <div class="login-wrap">
    <form class="card login-card" @submit.prevent="submit">
      <div class="login-brand">
        <span class="app-logo">DC</span>
        <span class="login-title">文档比对系统</span>
      </div>
      <p class="muted login-hint">请输入控制台访问口令</p>
      <input
        v-model="password"
        class="input login-input"
        type="password"
        placeholder="访问口令"
        autocomplete="current-password"
        autofocus
      />
      <p v-if="error" class="login-error">{{ error }}</p>
      <button class="btn btn-primary login-btn" type="submit" :disabled="submitting || !password">
        {{ submitting ? '登录中…' : '登录' }}
      </button>
    </form>
  </div>
</template>

<style scoped>
.login-wrap {
  flex: 1;
  display: flex;
  align-items: flex-start;
  justify-content: center;
  padding-top: 12vh;
}

.login-card {
  width: 360px;
  padding: 28px;
  display: flex;
  flex-direction: column;
}

.login-brand {
  display: flex;
  align-items: center;
  gap: 10px;
}

.login-title {
  font-size: 18px;
  font-weight: 600;
}

.login-hint {
  margin: 8px 0 16px;
}

.login-input {
  width: 100%;
}

.login-error {
  margin: 10px 0 0;
  color: var(--risk-high);
  font-size: 13px;
}

.login-btn {
  margin-top: 18px;
  width: 100%;
}
</style>
