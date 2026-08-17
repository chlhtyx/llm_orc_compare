<script setup lang="ts">
// 任务级模型 / OCR 调用记录卡片列表,展示每次调用的提示词 payload 与响应。
// 从 HistoryView 抽出共享:合同比对历史页与金额统计结果页复用同一展示。
// 数据来自 GET /api/v1/tasks/{taskId}/llm-calls(payload 中图片 base64 已脱敏,response 截断 64KB)。
import { ref } from 'vue'
import type { LlmCallItem } from '@/api/history'

defineProps<{ calls: LlmCallItem[] }>()

/** 展开后每条调用 payload/response 的折叠状态(id → 是否展开)。 */
const expandedCalls = ref<Record<number, boolean>>({})

/** 模型调用 / OCR 解析结果 kind → 中文标签(对齐后端 observability._COLLECTED_KINDS)。 */
const llmKindText: Record<string, string> = {
  'ocr': 'OCR 识别',
  'ocr-whole': '整页 OCR',
  'ocr-result': 'OCR 解析结果',
  'paddleocr': 'PaddleOCR',
  'judge': '辅助说明',
  'alignment': '条款对齐',
  'llm-diff': '整篇比对',
  'statement-column': '列定位',
  'statement-amount': '金额抽取',
}

function toggleCall(id: number): void {
  expandedCalls.value = { ...expandedCalls.value, [id]: !expandedCalls.value[id] }
}

/** status_code → CSS 类(2xx 绿、4xx 黄、5xx 红、null 灰)。 */
function statusClass(code: number | null): string {
  if (code == null) return 'http-fail'
  if (code >= 200 && code < 300) return 'http-ok'
  if (code >= 400 && code < 500) return 'http-4xx'
  return 'http-5xx'
}

function httpStatusText(code: number | null): string {
  return code == null ? '失败' : String(code)
}

function formatMs(ms: number | null): string {
  if (ms == null) return '—'
  if (ms < 1000) return `${ms} ms`
  return `${(ms / 1000).toFixed(2)} s`
}

function formatTime(iso: string | null): string {
  if (!iso) return '—'
  try {
    const d = new Date(iso)
    if (Number.isNaN(d.getTime())) return iso
    // 后端 UTC,按本地时区展示
    return d.toLocaleString('zh-CN', { hour12: false })
  } catch {
    return iso
  }
}

/** 把 payload/response 对象渲染成可读 JSON(图片已脱敏)。 */
function jsonPreview(value: unknown): string {
  try {
    return JSON.stringify(value, null, 2)
  } catch {
    return String(value)
  }
}
</script>

<template>
  <div v-if="!calls.length" class="muted">
    该任务没有已保存的模型 / OCR 记录(embedding 不记录;可能创建于本功能上线前)。
  </div>
  <ul v-else class="llm-call-list">
    <li v-for="call in calls" :key="call.id" class="llm-call-item">
      <div class="llm-call-head" @click="toggleCall(call.id)">
        <span class="llm-kind" :class="`kind-${call.kind}`">
          {{ llmKindText[call.kind] ?? call.kind }}
        </span>
        <span :class="['http-tag', statusClass(call.status_code)]">
          {{ httpStatusText(call.status_code) }}
        </span>
        <span class="muted small">#{{ call.attempt }}</span>
        <span class="muted small">{{ formatMs(call.elapsed_ms) }}</span>
        <span class="mono small ev-time">{{ formatTime(call.created_at) }}</span>
        <span class="muted small toggle-hint">
          {{ expandedCalls[call.id] ? '收起 ▲' : '展开 ▼' }}
        </span>
      </div>
      <div v-if="call.error" class="llm-call-err small">⚠ {{ call.error }}</div>
      <div v-if="expandedCalls[call.id]" class="llm-call-body">
        <div class="llm-block">
          <div class="llm-block-title muted small">
            {{ call.kind === 'ocr-result' ? '识别结果元数据' : '请求 payload(提示词,图片已脱敏)' }}
          </div>
          <pre class="llm-json">{{ jsonPreview(call.payload) }}</pre>
        </div>
        <div v-if="call.response" class="llm-block">
          <div class="llm-block-title muted small">
            {{ call.kind === 'ocr-result' ? '最终解析结果(文本预览受限，整条最多 64KB)' : '响应 response(截断 64KB)' }}
          </div>
          <pre class="llm-json">{{ jsonPreview(call.response) }}</pre>
        </div>
      </div>
    </li>
  </ul>
</template>

<style scoped>
.llm-call-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.llm-call-item {
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--surface);
  overflow: hidden;
}
.llm-call-head {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 12px;
  cursor: pointer;
  flex-wrap: wrap;
}
.llm-call-head:hover {
  background: var(--surface-2);
}
.llm-kind {
  font-size: 12px;
  font-weight: 600;
  padding: 2px 8px;
  border-radius: 4px;
  background: var(--surface-2);
  border: 1px solid var(--border);
}
.http-tag {
  font-family: var(--mono);
  font-size: 12px;
  font-weight: 600;
  padding: 1px 6px;
  border-radius: 3px;
}
.http-ok { background: var(--risk-low-bg); color: var(--risk-low); }
.http-4xx { background: var(--risk-medium-bg); color: var(--risk-medium); }
.http-5xx { background: var(--risk-high-bg); color: var(--risk-high); }
.http-fail { background: var(--risk-none-bg); color: var(--risk-none); }
.toggle-hint {
  margin-left: auto;
}
.llm-call-err {
  padding: 6px 12px;
  color: var(--risk-high);
  background: var(--risk-high-bg);
}
.llm-call-body {
  padding: 8px 12px 12px;
  border-top: 1px solid var(--border);
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.llm-block-title {
  margin-bottom: 4px;
}
.llm-json {
  margin: 0;
  max-height: 320px;
  overflow: auto;
  padding: 8px 10px;
  background: var(--surface-2);
  border: 1px solid var(--border);
  border-radius: 4px;
  font-family: var(--mono);
  font-size: 12px;
  line-height: 1.5;
  white-space: pre-wrap;
  word-break: break-all;
}
.ev-time {
  color: var(--text-muted);
}
</style>
