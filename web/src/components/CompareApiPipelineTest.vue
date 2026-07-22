<script setup lang="ts">
import { computed, onBeforeUnmount, ref } from 'vue'
import {
  ApiError,
  getCompareApiTest,
  getCompareApiTestImageUrl,
  submitCompareApiTest,
  type CompareApiTestInfo,
} from '@/api/compare'

const props = defineProps<{
  sourceFile: File | null
  targetFile: File | null
  enabled: boolean
  publicBaseUrl: string
}>()

const submitting = ref(false)
const info = ref<CompareApiTestInfo | null>(null)
const error = ref<string | null>(null)
const copyState = ref<'idle' | 'copied' | 'failed'>('idle')
let pollTimer: ReturnType<typeof setTimeout> | null = null
let copyTimer: ReturnType<typeof setTimeout> | null = null

const sourceValid = computed(() => !!props.sourceFile?.name.toLowerCase().endsWith('.docx'))
const targetValid = computed(() => !!props.targetFile?.name.toLowerCase().endsWith('.pdf'))
const running = computed(() => info.value?.status === 'pending' || info.value?.status === 'running')
const canSubmit = computed(
  () => props.enabled && sourceValid.value && targetValid.value && !submitting.value && !running.value,
)
const progressPercent = computed(() => Math.round((info.value?.progress ?? 0) * 100))
const resultClass = computed(() => `result-${info.value?.result?.change_status ?? 'clean'}`)
const resultLabel = computed(() => ({
  clean: '未发现内容变化',
  changed: '发现确认内容变化',
  needs_review: '存在待人工复核',
}[info.value?.result?.change_status ?? 'clean']))
const exampleBaseUrl = computed(
  () => props.publicBaseUrl.trim().replace(/\/$/, '') || 'https://compare.example.com',
)
const callExample = computed(() => [
  '# 1. 提交异步比对任务',
  `curl -X POST '${exampleBaseUrl.value}/api/v1/external/contractCompare' \\`,
  "  -H 'X-API-Key: <YOUR_API_KEY>' \\",
  "  -F 'source=@./original-contract.docx' \\",
  "  -F 'target=@./returned-contract.pdf' \\",
  "  -F 'document_no=DOC-2026-0001' \\",
  "  -F 'callback_url=https://business.example.com/callbacks/contract-compare' \\",
  "  -F 'callback_secret=<YOUR_CALLBACK_SECRET>'  # 可选,留空则回调不带签名",
  '',
  '# 2. 使用提交响应中的 task_id 查询结果',
  `curl -H 'X-API-Key: <YOUR_API_KEY>' \\`,
  `  '${exampleBaseUrl.value}/api/v1/external/contractCompare/<TASK_ID>'`,
  '',
  '# 3. 下载指定页高亮 PNG',
  `curl -H 'X-API-Key: <YOUR_API_KEY>' \\`,
  "  -o page-0001.png \\",
  `  '${exampleBaseUrl.value}/api/v1/external/contractCompare/<TASK_ID>/images/1'`,
].join('\n'))

function stopPolling(): void {
  if (pollTimer) clearTimeout(pollTimer)
  pollTimer = null
}

async function poll(taskId: string): Promise<void> {
  try {
    info.value = await getCompareApiTest(taskId)
    if (info.value.status === 'pending' || info.value.status === 'running') {
      pollTimer = setTimeout(() => void poll(taskId), 1000)
    }
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : `查询失败: ${(e as Error).message}`
    stopPolling()
  }
}

async function runTest(): Promise<void> {
  if (!props.sourceFile || !props.targetFile) return
  stopPolling()
  error.value = null
  info.value = null
  submitting.value = true
  try {
    info.value = await submitCompareApiTest(props.sourceFile, props.targetFile)
    await poll(info.value.task_id)
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : `提交失败: ${(e as Error).message}`
  } finally {
    submitting.value = false
  }
}

function imageUrl(pageNumber: number): string {
  return info.value ? getCompareApiTestImageUrl(info.value.task_id, pageNumber) : ''
}

async function copyCallExample(): Promise<void> {
  try {
    await navigator.clipboard.writeText(callExample.value)
    copyState.value = 'copied'
  } catch {
    copyState.value = 'failed'
  }
  if (copyTimer) clearTimeout(copyTimer)
  copyTimer = setTimeout(() => {
    copyState.value = 'idle'
  }, 1800)
}

onBeforeUnmount(() => {
  stopPolling()
  if (copyTimer) clearTimeout(copyTimer)
})
</script>

<template>
  <div class="pipeline-test">
    <div class="test-heading">
      <div>
        <h3 class="test-title">接口示例与执行</h3>
        <p class="muted test-desc">
          复用本页已选择的合同文件，验证外部 API 的标准比对、结果文本和全页高亮 PNG；测试任务不会发送回调。
        </p>
      </div>
      <span class="test-badge">异步接口</span>
    </div>

    <p v-if="!enabled" class="config-warning">
      请先在本页保存外部调用 API Key 和服务公开地址，再运行管线测试。
    </p>

    <details class="api-example">
      <summary>API 调用示例</summary>
      <div class="example-toolbar">
        <span>正式外部接口 · multipart/form-data</span>
        <button class="copy-button" type="button" @click="copyCallExample">
          {{ copyState === 'copied' ? '已复制' : copyState === 'failed' ? '复制失败' : '复制示例' }}
        </button>
      </div>
      <pre><code>{{ callExample }}</code></pre>
      <p class="example-note">
        请替换尖括号占位值；查询结果和下载图片时都必须携带同一个 X-API-Key。
      </p>
    </details>

    <div class="test-actions">
      <button class="btn" :disabled="!canSubmit" @click="runTest">
        {{ submitting ? '提交中…' : running ? `测试中 ${progressPercent}%` : '运行 API 管线测试' }}
      </button>
      <span v-if="info?.document_no" class="muted task-ref">{{ info.document_no }}</span>
    </div>

    <div v-if="running" class="progress-track" aria-label="管线测试进度">
      <div class="progress-fill" :style="{ width: `${progressPercent}%` }" />
    </div>
    <p v-if="running" class="muted stage-text">
      当前阶段：{{ info?.stage || '等待处理' }} · {{ progressPercent }}%
    </p>
    <p v-if="error || info?.error" class="err">{{ error || info?.error }}</p>

    <div v-if="info?.status === 'done' && info.result" class="test-result" :class="resultClass">
      <div class="result-heading">
        <strong>管线测试完成</strong>
        <span>{{ resultLabel }}</span>
      </div>
      <pre>{{ info.result.result_text }}</pre>

      <div v-if="info.highlight_images?.length" class="image-section">
        <h4>逐页高亮图片（{{ info.highlight_images.length }} 页）</h4>
        <div class="image-grid">
          <a
            v-for="item in info.highlight_images"
            :key="item.page_number"
            :href="imageUrl(item.page_number)"
            target="_blank"
            rel="noopener"
            class="image-card"
          >
            <img
              :src="imageUrl(item.page_number)"
              :alt="`第 ${item.page_number} 页高亮结果`"
              loading="lazy"
            />
            <span>
              第 {{ item.page_number }} 页
              <em>{{ item.has_highlight ? '有高亮' : '无高亮' }}</em>
            </span>
          </a>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.pipeline-test {
  margin-top: 22px;
  padding-top: 20px;
  border-top: 1px solid var(--border);
}
.test-heading,
.result-heading,
.test-actions {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}
.test-title {
  margin: 0 0 4px;
  font-size: 16px;
}
.test-desc {
  margin: 0 0 14px;
}
.test-badge {
  flex: 0 0 auto;
  padding: 3px 9px;
  border-radius: 999px;
  color: var(--primary);
  background: color-mix(in srgb, var(--primary) 10%, transparent);
  font-size: 12px;
  font-weight: 600;
}
.config-warning {
  margin: 0 0 14px;
  padding: 9px 11px;
  border-radius: var(--radius-sm);
  color: #8a5a00;
  background: #fff8e6;
  font-size: 13px;
}
.api-example {
  margin: 0 0 16px;
  overflow: hidden;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--surface-2);
}
.api-example summary {
  padding: 11px 13px;
  cursor: pointer;
  font-size: 13px;
  font-weight: 600;
}
.example-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  padding: 9px 12px;
  border-top: 1px solid var(--border);
  color: var(--text-muted);
  font-size: 12px;
}
.copy-button {
  flex: 0 0 auto;
  padding: 4px 9px;
  border: 1px solid var(--border);
  border-radius: 6px;
  color: var(--text);
  background: var(--surface);
  cursor: pointer;
  font: inherit;
}
.api-example pre {
  margin: 0;
  padding: 14px;
  overflow: auto;
  color: #dce7f5;
  background: #17202b;
  font: 12px/1.65 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  white-space: pre;
}
.example-note {
  margin: 0;
  padding: 9px 12px;
  color: var(--text-muted);
  font-size: 12px;
}
.test-actions {
  justify-content: flex-start;
}
.task-ref {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 12px;
}
.progress-track {
  height: 6px;
  margin-top: 14px;
  overflow: hidden;
  border-radius: 999px;
  background: var(--surface-2);
}
.progress-fill {
  height: 100%;
  border-radius: inherit;
  background: var(--primary);
  transition: width 0.25s ease;
}
.stage-text {
  margin: 6px 0 0;
  font-size: 12px;
}
.test-result {
  margin-top: 16px;
  padding: 14px;
  border: 1px solid var(--border);
  border-left-width: 4px;
  border-radius: var(--radius-sm);
  background: var(--surface-2);
}
.result-clean { border-left-color: var(--risk-clean); }
.result-changed { border-left-color: var(--risk-high); }
.result-needs_review { border-left-color: var(--risk-medium); }
.result-heading span {
  color: var(--text-muted);
  font-size: 12px;
}
.test-result pre {
  margin: 12px 0 0;
  overflow: auto;
  white-space: pre-wrap;
  word-break: break-word;
  font: inherit;
  font-size: 13px;
  line-height: 1.65;
}
.image-section h4 {
  margin: 18px 0 10px;
}
.image-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 10px;
}
.image-card {
  overflow: hidden;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  color: inherit;
  background: var(--surface);
  text-decoration: none;
}
.image-card img {
  display: block;
  width: 100%;
  aspect-ratio: 3 / 4;
  object-fit: cover;
  object-position: top;
  border-bottom: 1px solid var(--border);
}
.image-card span {
  display: flex;
  justify-content: space-between;
  gap: 6px;
  padding: 7px 8px;
  font-size: 12px;
}
.image-card em {
  color: var(--text-muted);
  font-style: normal;
}
.err {
  color: var(--risk-high);
  margin-top: 8px;
  font-size: 13px;
}
@media (max-width: 640px) {
  .test-heading { align-items: flex-start; }
  .image-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}
</style>
