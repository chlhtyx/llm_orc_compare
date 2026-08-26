<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import LlmCallList from '@/components/LlmCallList.vue'
import {
  ApiError,
  getTaskEvents,
  getTaskExternalCalls,
  getTaskLlmCalls,
  listTasks,
  reportRouteFor,
  redeliverCallback,
  type ExternalCallItem,
  type LlmCallItem,
  type TaskEventItem,
  type TaskKind,
  type TaskListItem,
} from '@/api/history'
import type { OverallRisk, TaskStatus } from '@/api/types'

const router = useRouter()

const items = ref<TaskListItem[]>([])
const total = ref(0)
const loading = ref(false)
const loadError = ref<string | null>(null)

// 重新推送回调:per-task 进行中状态 + 最近一次结果反馈
const redelivering = ref<Record<string, boolean>>({})
const redeliverMsg = ref<Record<string, string>>({})

const kindFilter = ref<TaskKind | ''>('')
const statusFilter = ref<TaskStatus | ''>('')
const searchQuery = ref('')
const PAGE_SIZE = 20
const page = ref(0) // 0 基

// 展开行:同一个任务下切换「时间线 / 模型调用 / 外部调用」面板
const selectedTaskId = ref<string | null>(null)
const detailTab = ref<'events' | 'llm-calls' | 'external-calls' | 'callback'>('events')
const selectedEvents = ref<TaskEventItem[]>([])
const selectedLlmCalls = ref<LlmCallItem[]>([])
const selectedExternalCalls = ref<ExternalCallItem[]>([])
const eventsLoading = ref(false)

const totalPages = computed(() => Math.max(1, Math.ceil(total.value / PAGE_SIZE)))

const kindText: Record<TaskKind, string> = {
  compare: '合同比对',
  raw: '无标注比对',
  statement: '对帐单统计',
}

/** 金额统计任务的单据类型细分('1'=发票 / '2'=对帐单,字符串枚举)。
 *  字段上线前的历史任务与内部提交未携带该值(null),不显示标签。 */
const documentTypeText: Record<'1' | '2', string> = {
  '1': '发票',
  '2': '对帐单',
}

const statusText: Record<TaskStatus, string> = {
  pending: '排队中',
  running: '处理中',
  done: '已完成',
  failed: '失败',
}

const callbackText: Record<NonNullable<TaskListItem['callback_status']>, string> = {
  pending: '待回调',
  success: '成功',
  failed: '失败',
}

/** 回调列单元格 title:汇总 HTTP 状态码/错误/时间,供悬浮查看交付明细。 */
function callbackTitle(item: TaskListItem): string {
  const parts: string[] = []
  if (item.callback_http_status != null) parts.push(`HTTP ${item.callback_http_status}`)
  if (item.callback_error) parts.push(item.callback_error)
  if (item.callback_at) parts.push(formatTime(item.callback_at))
  return parts.join(' · ') || '—'
}

/** 是否有任意过滤条件(搜索 / 类型 / 状态)在生效,用于区分空状态文案。 */
const hasActiveFilter = computed(
  () => !!searchQuery.value.trim() || !!kindFilter.value || !!statusFilter.value,
)

function riskText(risk: OverallRisk | null): string {
  switch (risk) {
    case 'high': return '高风险'
    case 'medium': return '中风险'
    case 'low': return '低风险'
    case 'changed': return '有变化'
    case 'clean': return '未篡改'
    case 'needs_review': return '待复核'
    default: return '—'
  }
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

function formatElapsed(s: number | null): string {
  if (s == null) return '—'
  if (s < 1) return `${(s * 1000).toFixed(0)} ms`
  if (s < 60) return `${s.toFixed(1)} s`
  const m = Math.floor(s / 60)
  const rest = (s - m * 60).toFixed(0)
  return `${m}m ${rest}s`
}

function fileNamesDisplay(item: TaskListItem): string {
  const targets = item.target_names || []
  if (item.kind === 'statement') {
    if (!targets.length) return '—'
    if (targets.length === 1) return targets[0]
    return `${targets[0]} 等 ${targets.length} 个文件`
  }
  const src = item.source_name || '—'
  const tgt = targets[0] || '—'
  return `${src} → ${tgt}`
}

/** 详情是否可展开(仅终态任务有里程碑/调用记录)。 */
function detailAvailable(item: TaskListItem): boolean {
  return item.status === 'done' || item.status === 'failed'
}

async function refresh(): Promise<void> {
  loading.value = true
  loadError.value = null
  try {
    const resp = await listTasks({
      kind: kindFilter.value || undefined,
      status: statusFilter.value || undefined,
      q: searchQuery.value || undefined,
      limit: PAGE_SIZE,
      offset: page.value * PAGE_SIZE,
    })
    items.value = resp.items
    total.value = resp.total
  } catch (e) {
    loadError.value = e instanceof ApiError ? e.message : `加载失败: ${(e as Error).message}`
  } finally {
    loading.value = false
  }
}

async function onFilterChange(): Promise<void> {
  page.value = 0
  collapseDetail()
  await refresh()
}

// 搜索框:防抖 350ms,触发时重置分页并折叠详情
let searchTimer: ReturnType<typeof setTimeout> | null = null
watch(searchQuery, () => {
  if (searchTimer) clearTimeout(searchTimer)
  searchTimer = setTimeout(() => {
    page.value = 0
    collapseDetail()
    void refresh()
  }, 350)
})

function clearSearch(): void {
  searchQuery.value = ''
}

async function prevPage(): Promise<void> {
  if (page.value <= 0) return
  page.value -= 1
  await refresh()
}

async function nextPage(): Promise<void> {
  if (page.value + 1 >= totalPages.value) return
  page.value += 1
  await refresh()
}

function viewReport(item: TaskListItem): void {
  router.push(reportRouteFor(item))
}

/** 整行点击:切换详情展开(默认时间线 tab)。终态任务才可展开。 */
async function toggleRow(item: TaskListItem): Promise<void> {
  if (!detailAvailable(item)) return
  if (selectedTaskId.value === item.task_id) {
    collapseDetail()
    return
  }
  await showEvents(item.task_id)
}

async function showEvents(taskId: string): Promise<void> {
  selectedTaskId.value = taskId
  detailTab.value = 'events'
  selectedEvents.value = []
  eventsLoading.value = true
  try {
    const resp = await getTaskEvents(taskId)
    selectedEvents.value = resp.items
  } catch (e) {
    selectedEvents.value = []
    console.warn('[history] load events failed:', e)
  } finally {
    eventsLoading.value = false
  }
}

async function showLlmCalls(taskId: string): Promise<void> {
  selectedTaskId.value = taskId
  detailTab.value = 'llm-calls'
  selectedLlmCalls.value = []
  eventsLoading.value = true
  try {
    const resp = await getTaskLlmCalls(taskId)
    selectedLlmCalls.value = resp.items
  } catch (e) {
    selectedLlmCalls.value = []
    console.warn('[history] load llm calls failed:', e)
  } finally {
    eventsLoading.value = false
  }
}

async function showExternalCalls(taskId: string): Promise<void> {
  selectedTaskId.value = taskId
  detailTab.value = 'external-calls'
  selectedExternalCalls.value = []
  eventsLoading.value = true
  try {
    const resp = await getTaskExternalCalls(taskId)
    selectedExternalCalls.value = resp.items
  } catch (e) {
    selectedExternalCalls.value = []
    console.warn('[history] load external calls failed:', e)
  } finally {
    eventsLoading.value = false
  }
}

function collapseDetail(): void {
  selectedTaskId.value = null
  detailTab.value = 'events'
  selectedEvents.value = []
  selectedLlmCalls.value = []
  selectedExternalCalls.value = []
}

/** 对已完成且配置了回调地址的任务重新推送一次回调。 */
async function redeliver(item: TaskListItem): Promise<void> {
  if (redelivering.value[item.task_id]) return
  redelivering.value = { ...redelivering.value, [item.task_id]: true }
  redeliverMsg.value = { ...redeliverMsg.value, [item.task_id]: '' }
  try {
    const r = await redeliverCallback(item.task_id)
    item.callback_status = r.callback_status
    item.callback_http_status = r.callback_http_status
    item.callback_error = r.callback_error
    item.callback_at = new Date().toISOString()
    redeliverMsg.value = {
      ...redeliverMsg.value,
      [item.task_id]: r.callback_status === 'success' ? '推送成功' : `推送失败:${r.callback_error ?? '未知错误'}`,
    }
  } catch (e) {
    redeliverMsg.value = {
      ...redeliverMsg.value,
      [item.task_id]: e instanceof ApiError ? e.message : `推送失败:${(e as Error).message}`,
    }
  } finally {
    redelivering.value = { ...redelivering.value, [item.task_id]: false }
    setTimeout(() => {
      redeliverMsg.value = { ...redeliverMsg.value, [item.task_id]: '' }
    }, 4000)
  }
}

/** 是否允许重新推送:任务已结束且有回调地址。 */
function canRedeliver(item: TaskListItem): boolean {
  return !!item.callback_url && (item.status === 'done' || item.status === 'failed')
}

onBeforeUnmount(() => {
  if (searchTimer) clearTimeout(searchTimer)
})

/** 外部接口 endpoint → 中文标签。 */
const externalEndpointText: Record<string, string> = {
  'contractCompare.submit': '提交比对',
  'contractCompare.result': '查询结果',
  'contractCompare.image': '取高亮图',
}

/** 把 64 位 sha256 指纹截成「前 8…后 8」便于阅读(仅展示,非明文)。 */
function shortFingerprint(sha: string | null): string {
  if (!sha) return '—'
  if (sha.length <= 16) return sha
  return `${sha.slice(0, 8)}…${sha.slice(-8)}`
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

/** 请求参数快照 → 「k=v」条目(null 值显示 —;对象/数组走 JSON)。 */
function paramEntries(params: Record<string, unknown> | null): Array<[string, string]> {
  if (!params) return []
  return Object.entries(params).map(([key, value]) => [
    key,
    value == null
      ? '—'
      : typeof value === 'string'
        ? value
        : JSON.stringify(value),
  ])
}

/** 把对象渲染成可读 JSON(回调 tab 展示 callback_payload;LLM 卡片的展示在 LlmCallList 组件内)。 */
function jsonPreview(value: unknown): string {
  try {
    return JSON.stringify(value, null, 2)
  } catch {
    return String(value)
  }
}

onMounted(refresh)
</script>

<template>
  <div class="history">
    <section class="card toolbar">
      <div class="head-row">
        <div class="head-left">
          <h2 class="page-title">合同对比记录</h2>
          <p class="muted page-desc">查看历史比对与统计任务,点击「查看报告」打开结果,或点击行查看时间线与模型调用。</p>
        </div>
        <div class="head-actions">
          <div class="search-box">
            <input
              v-model="searchQuery"
              class="input search-input"
              type="search"
              placeholder="搜索任务号 / 单据号 / 文件名"
            />
            <button
              v-if="searchQuery"
              class="search-clear"
              type="button"
              title="清除搜索"
              @click="clearSearch"
            >×</button>
          </div>
          <button class="btn" :disabled="loading" @click="refresh">
            {{ loading ? '刷新中…' : '刷新' }}
          </button>
        </div>
      </div>

      <div class="filters">
        <div class="field">
          <label>类型</label>
          <select v-model="kindFilter" class="input" @change="onFilterChange">
            <option value="">全部</option>
            <option value="compare">合同比对</option>
            <option value="raw">无标注比对</option>
            <option value="statement">对帐单统计</option>
          </select>
        </div>
        <div class="field">
          <label>状态</label>
          <select v-model="statusFilter" class="input" @change="onFilterChange">
            <option value="">全部</option>
            <option value="pending">排队中</option>
            <option value="running">处理中</option>
            <option value="done">已完成</option>
            <option value="failed">失败</option>
          </select>
        </div>
      </div>
    </section>

    <p v-if="loadError" class="card err">{{ loadError }}</p>

    <section class="card">
      <div v-if="!loading && items.length === 0" class="empty muted">
        {{ hasActiveFilter ? '没有符合条件的记录,试试调整搜索或筛选条件。' : '还没有合同对比记录。' }}
      </div>

      <div v-else class="table-wrap">
        <table class="task-table">
          <thead>
            <tr>
              <th>创建时间</th>
              <th>类型</th>
              <th>单据号 / 任务号</th>
              <th>文件</th>
              <th>状态</th>
              <th>结果</th>
              <th>回调</th>
              <th>耗时</th>
              <th class="col-actions">操作</th>
            </tr>
          </thead>
          <tbody>
            <template v-for="item in items" :key="item.task_id">
              <tr
                :class="['task-row', { 'row-expanded': selectedTaskId === item.task_id, 'row-clickable': detailAvailable(item) }]"
                @click="toggleRow(item)"
              >
                <td class="mono small">{{ formatTime(item.created_at) }}</td>
                <td>
                  <span class="kind-tag" :data-kind="item.kind">{{ kindText[item.kind] }}</span>
                  <span
                    v-if="item.kind === 'statement' && item.document_type != null"
                    class="doc-type-tag"
                    :data-doc-type="item.document_type"
                  >
                    {{ documentTypeText[item.document_type] }}
                  </span>
                </td>
                <td class="id-cell">
                  <div class="doc-no" :title="item.document_no ?? ''">
                    {{ item.document_no || '—' }}
                  </div>
                  <div class="task-id mono" :title="item.task_id">{{ item.task_id }}</div>
                </td>
                <td class="file-cell" :title="fileNamesDisplay(item)">
                  {{ fileNamesDisplay(item) }}
                </td>
                <td>
                  <span class="status-tag" :data-status="item.status">
                    {{ statusText[item.status] }}
                  </span>
                </td>
                <td>
                  <span v-if="item.kind === 'statement'">
                    {{ item.change_status ?? '—' }}
                  </span>
                  <span v-else :class="['risk-tag', item.overall_risk ? `risk-${item.overall_risk}` : '']">
                    {{ riskText(item.overall_risk) }}
                  </span>
                </td>
               <td :title="item.callback_status ? callbackTitle(item) : '—'">
                 <div class="cb-cell">
                  <span
                    v-if="item.callback_status"
                    :class="['cb-tag', `cb-${item.callback_status}`]"
                  >
                    {{ callbackText[item.callback_status] }}
                  </span>
                  <span v-else>—</span>
                  <div v-if="item.callback_url" class="cb-url mono small" :title="item.callback_url">
                    {{ item.callback_url }}
                  </div>
                 </div>
               </td>
               <td>{{ formatElapsed(item.elapsed) }}</td>
               <td class="col-actions" @click.stop>
                 <button class="chip" type="button" @click="viewReport(item)">查看报告</button>
                 <button
                   v-if="canRedeliver(item)"
                   class="chip chip-cb"
                   type="button"
                   :disabled="redelivering[item.task_id]"
                   :title="redeliverMsg[item.task_id] || '向回调地址重新推送一次结果'"
                   @click="redeliver(item)"
                 >
                   {{ redelivering[item.task_id] ? '推送中…' : '重新回调' }}
                 </button>
                 <button
                   v-if="detailAvailable(item)"
                   class="chip"
                   type="button"
                   @click="toggleRow(item)"
                 >
                   {{ selectedTaskId === item.task_id ? '收起' : '详情' }}
                 </button>
               </td>
              </tr>
              <tr v-if="selectedTaskId === item.task_id" class="event-row">
                <td colspan="9">
                  <div class="detail-panel">
                    <div class="detail-tabs">
                      <button
                        :class="['tab', { 'tab-active': detailTab === 'events' }]"
                        type="button"
                        @click="showEvents(item.task_id)"
                      >时间线</button>
                      <button
                        :class="['tab', { 'tab-active': detailTab === 'llm-calls' }]"
                        type="button"
                        @click="showLlmCalls(item.task_id)"
                      >模型 / OCR 记录</button>
                     <button
                       v-if="item.external_request"
                       :class="['tab', { 'tab-active': detailTab === 'external-calls' }]"
                       type="button"
                       @click="showExternalCalls(item.task_id)"
                     >外部调用</button>
                     <button
                       v-if="item.callback_url"
                       :class="['tab', { 'tab-active': detailTab === 'callback' }]"
                       type="button"
                       @click="detailTab = 'callback'"
                     >回调</button>
                   </div>
                   <div v-if="eventsLoading" class="muted detail-loading">
                     {{ detailTab === 'events' ? '加载时间线…' : detailTab === 'llm-calls' ? '加载模型 / OCR 记录…' : '加载外部调用…' }}
                   </div>
                    <template v-else-if="detailTab === 'events'">
                      <ol v-if="selectedEvents.length" class="event-list">
                        <li v-for="ev in selectedEvents" :key="ev.id">
                          <span class="ev-time mono small">{{ formatTime(ev.created_at) }}</span>
                          <span class="ev-stage">{{ ev.stage }}</span>
                          <span class="muted small">
                            progress {{ (ev.progress * 100).toFixed(0) }}%
                          </span>
                        </li>
                      </ol>
                      <p v-else class="muted">该任务没有已保存的里程碑事件(可能创建于本次持久化功能上线前)。</p>
                    </template>
                    <template v-else-if="detailTab === 'llm-calls'">
                      <LlmCallList :calls="selectedLlmCalls" />
                    </template>
                    <template v-else-if="detailTab === 'external-calls'">
                      <div v-if="!selectedExternalCalls.length" class="muted">
                        该任务没有已保存的外部接口调用记录(可能创建于本功能上线前)。
                      </div>
                      <ul v-else class="llm-call-list">
                        <li
                          v-for="call in selectedExternalCalls"
                          :key="call.id"
                          class="llm-call-item"
                        >
                          <div class="llm-call-head">
                            <span class="llm-kind kind-external">
                              {{ externalEndpointText[call.endpoint] ?? call.endpoint }}
                            </span>
                            <span :class="['http-tag', statusClass(call.status_code)]">
                              {{ httpStatusText(call.status_code) }}
                            </span>
                            <span class="muted small">{{ call.method }}</span>
                            <span class="muted small">{{ formatMs(call.elapsed_ms) }}</span>
                            <span class="mono small ev-time">{{ formatTime(call.created_at) }}</span>
                          </div>
                          <div v-if="call.error" class="llm-call-err small">⚠ {{ call.error }}</div>
                          <div class="ext-meta small">
                            <span class="muted">IP:</span>
                            <span class="mono">{{ call.client_ip || '—' }}</span>
                            <span class="muted">Key:</span>
                            <span class="mono" :title="call.api_key_sha256 ?? ''">
                              {{ shortFingerprint(call.api_key_sha256) }}
                            </span>
                            <span v-if="call.document_no" class="muted">单据号:</span>
                            <span v-if="call.document_no">{{ call.document_no }}</span>
                            <span v-if="call.content_length != null" class="muted">
                              大小:{{ call.content_length }} B
                            </span>
                            <span class="muted">请求 ID:</span>
                            <span class="mono">{{ call.request_id }}</span>
                          </div>
                          <div
                            v-if="paramEntries(call.request_params).length"
                            class="ext-params small"
                          >
                            <span class="muted">请求参数:</span>
                            <span
                              v-for="[key, val] in paramEntries(call.request_params)"
                              :key="key"
                              class="ext-param"
                            >
                              <span class="muted">{{ key }}</span>
                              <span class="mono">{{ val }}</span>
                            </span>
                          </div>
                        </li>
                     </ul>
                    </template>
                    <template v-else-if="detailTab === 'callback'">
                      <div class="cb-detail">
                        <div class="cb-detail-row small">
                          <span class="muted">回调地址</span>
                          <span class="mono cb-detail-url" :title="item.callback_url ?? ''">
                            {{ item.callback_url || '—' }}
                          </span>
                        </div>
                        <div class="cb-detail-row small">
                          <span class="muted">交付状态</span>
                          <span
                            v-if="item.callback_status"
                            :class="['cb-tag', `cb-${item.callback_status}`]"
                          >
                            {{ callbackText[item.callback_status] }}
                          </span>
                          <span v-else>—</span>
                          <span v-if="item.callback_http_status != null" class="mono muted">
                            HTTP {{ item.callback_http_status }}
                          </span>
                          <span v-if="item.callback_at" class="mono muted">
                            {{ formatTime(item.callback_at) }}
                          </span>
                        </div>
                        <div v-if="item.callback_error" class="cb-detail-row small">
                          <span class="muted">失败原因</span>
                          <span class="err">{{ item.callback_error }}</span>
                        </div>
                        <div v-if="redeliverMsg[item.task_id]" class="cb-detail-row small">
                          <span class="muted">重推反馈</span>
                          <span>{{ redeliverMsg[item.task_id] }}</span>
                        </div>
                        <div class="cb-detail-actions">
                          <button
                            class="chip chip-cb"
                            type="button"
                            :disabled="redelivering[item.task_id]"
                            @click="redeliver(item)"
                          >
                            {{ redelivering[item.task_id] ? '推送中…' : '重新回调推送' }}
                          </button>
                        </div>
                        <div v-if="item.callback_payload" class="llm-block">
                          <div class="llm-block-title muted small">回调 payload(首次交付业务内容,重推时原样发送)</div>
                          <pre class="llm-json">{{ jsonPreview(item.callback_payload) }}</pre>
                        </div>
                        <p v-else class="muted small">无已保存的回调 payload(可能创建于本功能上线前,重推时仅发送最小事件信封)。</p>
                      </div>
                    </template>
                    <p v-if="item.error" class="err small">错误: {{ item.error }}</p>
                  </div>
                </td>
              </tr>
            </template>
          </tbody>
        </table>
      </div>

      <div v-if="total > PAGE_SIZE" class="pager">
        <button class="chip" :disabled="page === 0" @click="prevPage">上一页</button>
        <span class="muted small">第 {{ page + 1 }} / {{ totalPages }} 页 · 共 {{ total }} 条</span>
        <button class="chip" :disabled="page + 1 >= totalPages" @click="nextPage">下一页</button>
      </div>
    </section>
  </div>
</template>

<style scoped>
/* 本页表格列较多,突破全局 .app-main 的 1080px 限制,
   让卡片按视口宽度的 98% 居中显示,留出 1% 两侧呼吸空间。 */
.history {
  width: 98vw;
  margin-left: calc(50% - 49vw);
  margin-right: calc(50% - 49vw);
}
.toolbar {
  padding: 18px 24px 16px;
}
.head-row {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 16px;
  margin-bottom: 14px;
}
.head-left {
  min-width: 0;
}
.head-actions {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-shrink: 0;
}
.page-title {
  margin: 0 0 4px;
  font-size: 18px;
}
.page-desc {
  margin: 0;
  font-size: 13px;
}

/* —— 搜索框 —— */
.search-box {
  position: relative;
  display: inline-flex;
  align-items: center;
}
.search-input {
  width: 280px;
  padding-right: 30px;
}
.search-clear {
  position: absolute;
  right: 6px;
  top: 50%;
  transform: translateY(-50%);
  width: 20px;
  height: 20px;
  line-height: 1;
  padding: 0;
  border: none;
  border-radius: 50%;
  background: var(--surface-2);
  color: var(--text-muted);
  font-size: 16px;
  cursor: pointer;
}
.search-clear:hover {
  background: var(--border);
  color: var(--text);
}

.filters {
  display: flex;
  gap: 16px;
  flex-wrap: wrap;
}
.filters .field {
  min-width: 160px;
}
.empty {
  padding: 40px 0;
  text-align: center;
}
.table-wrap {
  overflow-x: auto;
}
table.task-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
}
.task-table th,
.task-table td {
  padding: 10px 12px;
  text-align: left;
  border-bottom: 1px solid var(--border);
  vertical-align: top;
}
.task-table th {
  font-weight: 600;
  color: var(--text-muted);
  background: var(--surface-2);
  font-size: 12px;
  letter-spacing: 0.02em;
  white-space: nowrap;
}
.task-table tbody tr.task-row {
  transition: background 0.1s;
}
.task-table tbody tr.task-row:hover {
  background: var(--surface-2);
}
.task-table tbody tr.row-clickable {
  cursor: pointer;
}
/* 当前展开行:用主色左边缘 + 浅底标识 */
.task-table tbody tr.row-expanded {
  background: rgba(43, 95, 214, 0.04);
  box-shadow: inset 3px 0 0 var(--primary);
}
.task-table .col-actions {
  white-space: nowrap;
  text-align: right;
}
.task-table .col-actions .chip {
  margin-left: 6px;
}
.file-cell {
  max-width: 260px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
/* 单据号 / 任务号合列:主行 document_no,副行 task_id(muted) */
.id-cell {
  max-width: 170px;
}
.id-cell .doc-no {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.id-cell .task-id {
  font-size: 11px;
  color: var(--text-muted);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.small {
  font-size: 12px;
}
.mono {
  font-family: var(--mono);
}

.kind-tag {
  display: inline-block;
  padding: 2px 8px;
  border-radius: 999px;
  font-size: 12px;
  background: var(--surface-2);
  color: var(--text-muted);
}
.kind-tag[data-kind='compare'] { background: var(--primary); color: #fff; }
.kind-tag[data-kind='raw'] { background: var(--risk-low-bg); color: var(--risk-low); }
.kind-tag[data-kind='statement'] { background: var(--risk-medium-bg); color: var(--risk-medium); }

/* 金额统计任务的单据类型细分(发票 / 对帐单),与 kind 标签同列分行展示 */
.doc-type-tag {
  display: block;
  width: fit-content;
  margin-top: 4px;
  padding: 1px 8px;
  border-radius: 999px;
  font-size: 11px;
  border: 1px solid var(--border);
  background: var(--surface-2);
  color: var(--text-muted);
}
.doc-type-tag[data-doc-type='2'] {
  border-color: var(--primary);
  color: var(--primary);
}

.status-tag {
  display: inline-block;
  padding: 2px 8px;
  border-radius: 999px;
  font-size: 12px;
  background: var(--surface-2);
  color: var(--text-muted);
}
.status-tag[data-status='done'] { background: var(--risk-low-bg); color: var(--risk-low); }
.status-tag[data-status='running'] { background: var(--risk-medium-bg); color: var(--risk-medium); }
.status-tag[data-status='failed'] { background: var(--risk-high-bg); color: var(--risk-high); }
.status-tag[data-status='pending'] { background: var(--surface-2); color: var(--text-muted); }

/* 回调状态标签 */
.cb-tag {
  display: inline-block;
  padding: 2px 8px;
  border-radius: 999px;
  font-size: 12px;
}
.cb-success { background: var(--risk-low-bg); color: var(--risk-low); }
.cb-failed { background: var(--risk-high-bg); color: var(--risk-high); }
.cb-pending { background: var(--surface-2); color: var(--text-muted); }
/* 回调地址 + 重新推送按钮 */
.cb-cell { display: flex; flex-direction: column; gap: 2px; }
.cb-url {
  max-width: 200px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--text-muted);
}
.chip-cb { border-color: var(--accent, var(--risk-medium)); }
/* 回调详情面板 */
.cb-detail { display: flex; flex-direction: column; gap: 8px; }
.cb-detail-row { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.cb-detail-row > .muted:first-child { min-width: 64px; }
.cb-detail-url {
  max-width: 100%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.cb-detail-actions { display: flex; gap: 8px; }

.risk-tag { color: var(--text-muted); }
.risk-tag.risk-high { color: var(--risk-high); font-weight: 600; }
.risk-tag.risk-medium { color: var(--risk-medium); font-weight: 600; }
.risk-tag.risk-low { color: var(--risk-low); }
.risk-tag.risk-changed { color: var(--risk-medium); font-weight: 600; }
.risk-tag.risk-needs_review { color: var(--risk-medium); font-weight: 600; }
.risk-tag.risk-clean { color: var(--risk-clean); }

/* —— 展开详情面板 —— */
.event-row td {
  background: var(--surface-2);
  padding: 0;
}
.detail-panel {
  padding: 14px 16px;
}
.detail-tabs {
  display: flex;
  gap: 4px;
  margin-bottom: 12px;
  border-bottom: 1px solid var(--border);
}
.detail-tabs .tab {
  padding: 6px 14px;
  border: none;
  background: transparent;
  color: var(--text-muted);
  font-size: 13px;
  font-weight: 500;
  cursor: pointer;
  border-bottom: 2px solid transparent;
  margin-bottom: -1px;
}
.detail-tabs .tab:hover {
  color: var(--text);
}
.detail-tabs .tab-active {
  color: var(--primary);
  border-bottom-color: var(--primary);
}
.detail-loading {
  padding: 8px 0;
}
.event-list {
  margin: 0;
  padding-left: 18px;
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.event-list li {
  display: flex;
  gap: 12px;
  align-items: center;
}
.ev-time {
  min-width: 150px;
  color: var(--text-muted);
}
.ev-stage {
  font-family: var(--mono);
  background: var(--surface);
  border: 1px solid var(--border);
  padding: 1px 6px;
  border-radius: 4px;
}

/* —— 调用记录卡片(外部调用 tab 复用;LLM 调用卡片样式在 LlmCallList 组件内) —— */
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
.llm-call-err {
  padding: 6px 12px;
  color: var(--risk-high);
  background: var(--risk-high-bg);
}
.ext-params {
  display: flex;
  flex-wrap: wrap;
  gap: 2px 14px;
  padding: 0 12px 6px;
}
.ext-param {
  max-width: 100%;
  overflow-wrap: anywhere;
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

.pager {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 12px;
  margin-top: 14px;
}
.err {
  color: var(--risk-high);
}

@media (max-width: 720px) {
  .head-row {
    flex-direction: column;
  }
  .head-actions {
    width: 100%;
  }
  .search-input {
    width: 100%;
    flex: 1;
  }
}
</style>
