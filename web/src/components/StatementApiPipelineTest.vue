<script setup lang="ts">
import { computed, onBeforeUnmount, ref } from 'vue'
import {
  ApiError,
  getStatementApiTest,
  submitStatementApiTest,
  type StatementApiTestInfo,
} from '@/api/statement'

const props = defineProps<{
  targetFiles: File[]
  enabled: boolean
  publicBaseUrl: string
}>()

const submitting = ref(false)
const info = ref<StatementApiTestInfo | null>(null)
const error = ref<string | null>(null)
const copyState = ref<'idle' | 'copied' | 'failed'>('idle')
const targetUrlText = ref('')
let pollTimer: ReturnType<typeof setTimeout> | null = null
let copyTimer: ReturnType<typeof setTimeout> | null = null

const filesValid = computed(
  () => props.targetFiles.every((file) => file.name.toLowerCase().endsWith('.pdf')),
)
const targetUrls = computed(() => targetUrlText.value.split(/[\n,，]/).map((url) => url.trim()).filter(Boolean))
const urlsValid = computed(() => targetUrls.value.every((url) => {
  try {
    const parsed = new URL(url)
    return parsed.protocol === 'http:' || parsed.protocol === 'https:'
  } catch {
    return false
  }
}))
const hasTarget = computed(() => props.targetFiles.length > 0 || targetUrls.value.length > 0)
const running = computed(() => info.value?.status === 'pending' || info.value?.status === 'running')
const canSubmit = computed(
  () => props.enabled && hasTarget.value && filesValid.value && urlsValid.value && !submitting.value && !running.value,
)
const progressPercent = computed(() => Math.round((info.value?.progress ?? 0) * 100))
const exampleBaseUrl = computed(
  () => props.publicBaseUrl.trim().replace(/\/$/, '') || 'https://compare.example.com',
)
const verdictLabel = computed(() => ({
  clean: '声明合计与代码实算一致',
  changed: '声明合计与代码实算不一致',
  needs_review: '存在待人工复核项',
}[info.value?.verdict ?? 'needs_review']))
const resultClass = computed(() => `result-${info.value?.verdict ?? 'needs_review'}`)
const callExample = computed(() => {
  const endpoint = `${exampleBaseUrl.value}/api/v1/external/amountStat`
  const taskId = '<TASK_ID>'
  return [
    '# ===== 1. 异步模式(默认: sync 缺省或 false) =====',
    '# 提交后立即返回 task_id；异步模式必须提供 callback_url。',
    `curl -X POST '${endpoint}' \\`,
    "  -H 'X-API-Key: <YOUR_API_KEY>' \\",
    "  -F 'document_no=STMT-2026-0001' \\",
    "  -F 'document_type=1' \\",
    "  -F 'callback_url=https://business.example.com/callbacks/amount-stat' \\",
    "  -F 'target=@./statement-01.pdf' \\",
    "  -F 'target=@./statement-02.pdf'",
    '# 返回(HTTP 202):',
    '# { "task_id": "a1b2c3d4e5f6", "document_no": "STMT-2026-0001", "document_type": "1", "status": "pending" }',
    '',
    '# 用 task_id 主动查询结果：',
    `curl -H 'X-API-Key: <YOUR_API_KEY>' '${endpoint}/${taskId}'`,
    '',
    '# ===== 2. URL 数组提交(可与 target 文件混合) =====',
    '# target_urls 是一个 JSON 字符串数组；不要将 Content-Type 手动设为 multipart/form-data。',
    `curl -X POST '${endpoint}' \\`,
    "  -H 'X-API-Key: <YOUR_API_KEY>' \\",
    "  -F 'document_no=STMT-2026-0002' \\",
    "  -F 'document_type=2' \\",
    "  -F 'callback_url=https://business.example.com/callbacks/amount-stat' \\",
    "  -F 'target_urls=[\"https://files.example.com/statement-01.pdf\",\"https://files.example.com/statement-02.pdf\"]'",
    '# 若需混合提交，在上方命令再追加：-F \'target=@./local-statement.pdf\'。',
    '# document_type 可省略(默认 1=发票);统计对象为对帐单时传 2。',
    '',
    '# ===== 3. 同步模式(sync=true) =====',
    '# 请求会等待 OCR、表格抽取与确定性求和完成；callback_url 可省略。',
    `curl -X POST '${endpoint}' \\`,
    "  -H 'X-API-Key: <YOUR_API_KEY>' \\",
    "  -F 'document_no=STMT-2026-0001' \\",
    "  -F 'sync=true' \\",
    "  -F 'target=@./statement-01.pdf' \\",
    "  -F 'target=@./statement-02.pdf'",
    '',
    '# 同步提交 / 异步查询完成时(HTTP 200)：',
    '# {',
    `#   "task_id": "${taskId}",`,
    '#   "document_type": "1",                   // "1"=发票(默认) / "2"=对帐单',
    '#   "status": "done",',
    '#   "grand_total": 100000.0,',
    '#   "verdict": "clean",                 // clean / changed / needs_review',
    '#   "file_totals": [',
    '#     { "file_index": 0, "file_name": "statement-01.pdf", "total_amount": 60000.0, "error": null },',
    '#     { "file_index": 1, "file_name": "statement-02.pdf", "total_amount": 40000.0, "error": null }',
    '#   ],',
    '#   "total_files": 2,',
    '#   "total_tables": 2,',
    '#   "total_items": 5,',
    '#   "result_url": ".../amountStat/<TASK_ID>"',
    '# }',
    '# 金额始终由服务端以 Decimal 确定性求和；LLM 只在必要时指认金额列。',
    '',
    '# ===== 4. 异步完成回调 =====',
    '# 服务端向 callback_url POST，并携带 Content-Type 和 X-Event-Id 请求头。',
    '# 成功事件 event_type="statement.summary.completed"；失败事件为 "statement.summary.failed"。',
  ].join('\n')
})

function stopPolling(): void {
  if (pollTimer) clearTimeout(pollTimer)
  pollTimer = null
}

// —— 金额统计 LLM 提示词原文(对齐后端 statement/llm_column_detect.py 与 llm_amount_extract.py)——
// LLM 仅作兜底:列定位在启发式抽空时触发,金额抽取在列指认仍抽空时触发(典型为发票纯数字金额);
// 两者只返回 JSON,求和始终由服务端代码以 Decimal 完成,抽取结果须过 grounding 逐字溯源校验。
const COLUMN_SYSTEM_PROMPT = [
  '你是表格结构分析助手。本任务只统计【含税金额】。任务:看图识别表格中哪些列是金额列。',
  '只返回 JSON,不做任何数值识别或计算。',
  '列角色:tax_inclusive=价税合计/含税/含税金额(本任务唯一目标,优先指认),',
  'amount=金额/不含税金额(税前,需排除), tax=税额/税款(需排除),',
  'paid=已付/实付, unpaid=未付/应付, total=合计/小计。',
  '若同时存在 tax_inclusive 与 amount/tax 列,只指认 tax_inclusive,不要指认 amount 与 tax(避免重复计入)。',
].join('\n')
const COLUMN_USER_TEMPLATE = [
  '下表表头各列如下(0 基索引):',
  '{headers_block}',
  '',
  '请只返回 JSON,格式为 {"columns": [{"index": 0, "role": "tax_inclusive"}]}。',
  '只指认金额相关的列;如果有含税/价税合计列,只指认它,跳过同表的金额(不含税)与税额列;',
  '如果没有金额列,返回 {"columns": []}。',
  '不要识别具体数值,不要做任何加法或求和。',
].join('\n')
const AMOUNT_SYSTEM_PROMPT = [
  '你是发票/收据/对账单的金额抽取助手。本任务只统计【含税金额】。',
  '任务:从给定表格中抽取【含税金额】类列(价税合计/含税/含税金额)里每个数据行的金额数值。',
  '只返回 JSON,不做任何求和或计算。',
  '若表中同时存在「金额(不含税)」列与「税额」列,只抽含税金额列,不要抽这两列,避免与含税金额重复计入。',
  '不要抽数量、单价、税率、百分比、日期、行号、编号。',
].join('\n')
const AMOUNT_USER_TEMPLATE = [
  '表格表头如下:',
  '{headers_block}',
  '',
  '表格数据(每行用 | 分隔):',
  '{rows_block}',
  '',
  '下方是本页 OCR 识别出的原文,你返回的每个金额必须能在其中逐字找到:',
  '---BEGIN GROUNDING---',
  '{grounding}',
  '---END GROUNDING---',
  '',
  '请只返回 JSON,格式为 {"items": [{"amount": "1680.00"}]}。',
  'amount 为含税金额数字字符串(可含千分位/小数,不要带 ¥/元 等符号)。',
  '只抽含税/价税合计列的数据行金额,跳过合计/小计行;',
  '若同表有金额(不含税)列与税额列,不要抽它们(会与含税金额重复);',
  '若没有任何含税金额,返回 {"items": []}。',
].join('\n')

async function poll(taskId: string): Promise<void> {
  try {
    info.value = await getStatementApiTest(taskId)
    if (info.value.status === 'pending' || info.value.status === 'running') {
      pollTimer = setTimeout(() => void poll(taskId), 1000)
    }
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : `查询失败: ${(e as Error).message}`
    stopPolling()
  }
}

async function runTest(): Promise<void> {
  if (!hasTarget.value || !filesValid.value || !urlsValid.value) return
  stopPolling()
  error.value = null
  info.value = null
  submitting.value = true
  try {
    info.value = await submitStatementApiTest({
      targets: props.targetFiles,
      targetUrls: targetUrls.value,
    })
    await poll(info.value.task_id)
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : `提交失败: ${(e as Error).message}`
  } finally {
    submitting.value = false
  }
}

async function copyCallExample(): Promise<void> {
  try {
    await navigator.clipboard.writeText(callExample.value)
    copyState.value = 'copied'
  } catch {
    copyState.value = 'failed'
  }
  if (copyTimer) clearTimeout(copyTimer)
  copyTimer = setTimeout(() => (copyState.value = 'idle'), 1800)
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
          使用上传 PDF 或 URL 验证外部 API 的 OCR、表格抽取、金额列定位和确定性求和；测试任务不会发送回调。
        </p>
      </div>
      <span class="test-badge">异步接口</span>
    </div>

    <p v-if="!enabled" class="config-warning">
      请先在本页保存服务公开地址、上传上限和 OCR 默认引擎，再运行管线测试。
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
      <p class="example-note">请替换尖括号占位值；查询结果时必须携带同一个 X-API-Key。</p>
    </details>

    <div class="field url-field">
      <label for="statement-target-urls">PDF URL（可选，可与文件混合）</label>
      <textarea
        id="statement-target-urls"
        v-model="targetUrlText"
        class="input url-input"
        rows="3"
        placeholder="https://files.example.com/statement-01.pdf&#10;https://files.example.com/statement-02.pdf"
      />
      <span class="hint">每行或逗号分隔一个 HTTP/HTTPS 地址；文件名由服务端下载后校验必须为 .pdf。</span>
      <span v-if="targetUrlText.trim() && !urlsValid" class="err">每个 URL 必须是有效的 HTTP/HTTPS 地址</span>
    </div>

    <div class="test-actions">
      <button class="btn" :disabled="!canSubmit" @click="runTest">
        {{ submitting ? '提交中…' : running ? `测试中 ${progressPercent}%` : '运行 API 管线测试' }}
      </button>
      <span v-if="info?.document_no" class="muted task-ref">{{ info.document_no }}</span>
    </div>

    <div v-if="running" class="progress-track" aria-label="管线测试进度">
      <div class="progress-fill" :style="{ width: `${progressPercent}%` }" />
    </div>
    <p v-if="running" class="muted stage-text">当前阶段：{{ info?.stage || '等待处理' }} · {{ progressPercent }}%</p>
    <p v-if="error || info?.error" class="err">{{ error || info?.error }}</p>

    <section v-if="info?.status === 'done' && info.verdict" class="test-result" :class="resultClass">
      <div class="result-heading">
        <div class="result-status">
          <strong>管线测试完成</strong>
          <span>{{ verdictLabel }}</span>
        </div>
        <RouterLink :to="`/statement/report/${info.task_id}`" class="report-link">查看完整报告</RouterLink>
      </div>
      <div class="summary-grid">
        <div><small>总金额</small><strong>{{ info.grand_total ?? 0 }}</strong></div>
        <div><small>PDF 文件</small><strong>{{ info.total_files ?? 0 }}</strong></div>
        <div><small>识别表格</small><strong>{{ info.total_tables ?? 0 }}</strong></div>
        <div><small>金额明细</small><strong>{{ info.total_items ?? 0 }}</strong></div>
      </div>
      <div v-if="info.file_totals?.length" class="file-totals">
        <strong>每个文件的金额合计</strong>
        <dl>
          <template v-for="file in info.file_totals" :key="file.file_index">
            <dt>{{ file.file_name || `文件 ${file.file_index + 1}` }}</dt>
            <dd>{{ file.error ? `统计失败：${file.error}` : file.total_amount }}</dd>
          </template>
        </dl>
      </div>
      <ul v-if="info.reasons?.length" class="reasons">
        <li v-for="reason in info.reasons" :key="reason">{{ reason }}</li>
      </ul>
    </section>

    <!-- 金额统计 LLM 兜底调用的提示词原文(静态展示,对齐后端 statement 模块) -->
    <details class="api-example llm-example">
      <summary>LLM 使用提示词</summary>
      <div class="example-toolbar">
        <span>LLM 仅兜底 · 只返回 JSON · 求和由代码 Decimal 完成 · 抽取须过 grounding 校验</span>
      </div>
      <div class="prompt-group">
        <h4>金额列定位（statement-column，启发式列定位抽空时触发）</h4>
        <div class="prompt-item">
          <span class="prompt-role">system</span>
          <pre><code>{{ COLUMN_SYSTEM_PROMPT }}</code></pre>
        </div>
        <div class="prompt-item">
          <span class="prompt-role">user 模板</span>
          <pre><code>{{ COLUMN_USER_TEMPLATE }}</code></pre>
        </div>
      </div>
      <div class="prompt-group">
        <h4>金额抽取（statement-amount，列指认仍抽空时触发，典型为发票纯数字金额）</h4>
        <div class="prompt-item">
          <span class="prompt-role">system</span>
          <pre><code>{{ AMOUNT_SYSTEM_PROMPT }}</code></pre>
        </div>
        <div class="prompt-item">
          <span class="prompt-role">user 模板</span>
          <pre><code>{{ AMOUNT_USER_TEMPLATE }}</code></pre>
        </div>
      </div>
      <p class="example-note">
        花括号占位（headers_block / rows_block / grounding）在每次调用时由服务端填充；表格截图以图片随消息发送。历史任务的完整调用 payload 可在比对记录页的「模型 / OCR 记录」中查看。
      </p>
    </details>
  </div>
</template>

<style scoped>
.pipeline-test { margin-top: 22px; padding-top: 20px; border-top: 1px solid var(--border); }
.test-heading, .result-heading, .test-actions { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.test-title { margin: 0 0 4px; font-size: 16px; }
.test-desc { margin: 0 0 14px; }
.test-badge { flex: 0 0 auto; padding: 3px 9px; border-radius: 999px; color: var(--primary); background: color-mix(in srgb, var(--primary) 10%, transparent); font-size: 12px; font-weight: 600; }
.config-warning { margin: 0 0 14px; padding: 9px 11px; border-radius: var(--radius-sm); color: #8a5a00; background: #fff8e6; font-size: 13px; }
.api-example { margin: 0 0 16px; overflow: hidden; border: 1px solid var(--border); border-radius: var(--radius-sm); background: var(--surface-2); }
.api-example summary { padding: 11px 13px; cursor: pointer; font-size: 13px; font-weight: 600; }
.example-toolbar { display: flex; align-items: center; justify-content: space-between; gap: 10px; padding: 9px 12px; border-top: 1px solid var(--border); color: var(--text-muted); font-size: 12px; }
.copy-button { flex: 0 0 auto; padding: 4px 9px; border: 1px solid var(--border); border-radius: 6px; color: var(--text); background: var(--surface); cursor: pointer; font: inherit; }
.api-example pre { margin: 0; padding: 14px; overflow: auto; color: #dce7f5; background: #17202b; font: 12px/1.65 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; white-space: pre; }
.example-note { margin: 0; padding: 9px 12px; color: var(--text-muted); font-size: 12px; }
.url-field { margin: 0 0 16px; }.url-input { min-height: 78px; resize: vertical; line-height: 1.45; }.test-actions { justify-content: flex-start; }
.task-ref { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; }
.progress-track { height: 6px; margin-top: 14px; overflow: hidden; border-radius: 999px; background: var(--surface-2); }
.progress-fill { height: 100%; border-radius: inherit; background: var(--primary); transition: width .25s ease; }
.stage-text { margin: 6px 0 0; font-size: 12px; }
.err { margin: 8px 0 0; color: var(--risk-high); font-size: 13px; }
.test-result { margin-top: 16px; padding: 14px; border: 1px solid var(--border); border-left-width: 4px; border-radius: var(--radius-sm); background: var(--surface-2); }
.result-clean { border-left-color: var(--risk-clean); }
.result-changed { border-left-color: var(--risk-high); }
.result-needs_review { border-left-color: var(--risk-medium); }
.result-status { display: flex; align-items: center; gap: 10px; }.result-status span { color: var(--text-muted); font-size: 12px; }.report-link { color: var(--primary); font-size: 13px; font-weight: 600; text-decoration: none; }.report-link:hover { text-decoration: underline; }
.summary-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 10px; margin-top: 14px; }
.summary-grid > div { padding: 10px; border: 1px solid var(--border); border-radius: 6px; background: var(--surface); }
.summary-grid small, .summary-grid strong { display: block; }
.summary-grid small { color: var(--text-muted); font-size: 11px; }
.summary-grid strong { margin-top: 3px; font-variant-numeric: tabular-nums; font-size: 16px; }
.file-totals { margin-top: 14px; font-size: 13px; }.file-totals dl { display: grid; grid-template-columns: minmax(0, 1fr) max-content; gap: 5px 16px; margin: 8px 0 0; }.file-totals dt { overflow: hidden; color: var(--text-muted); text-overflow: ellipsis; white-space: nowrap; }.file-totals dd { margin: 0; font-variant-numeric: tabular-nums; }
.reasons { margin: 14px 0 0; padding-left: 18px; color: var(--text-muted); font-size: 13px; }
.llm-example { margin-top: 16px; }
.prompt-group { padding: 12px 12px 0; }
.prompt-group h4 { margin: 0 0 8px; font-size: 13px; }
.prompt-item { margin-bottom: 10px; }
.prompt-role { display: inline-block; margin-bottom: 4px; padding: 1px 8px; border-radius: 999px; border: 1px solid var(--border); color: var(--text-muted); background: var(--surface); font-size: 11px; font-weight: 600; }
.prompt-item pre { margin: 0; padding: 12px 14px; overflow: auto; color: #dce7f5; background: #17202b; font: 12px/1.65 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; white-space: pre-wrap; word-break: break-word; }
@media (max-width: 620px) { .test-heading { align-items: flex-start; } .summary-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
</style>
