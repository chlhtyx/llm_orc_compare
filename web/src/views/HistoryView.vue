<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import {
  ApiError,
  getTaskEvents,
  listTasks,
  reportRouteFor,
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

const kindFilter = ref<TaskKind | ''>('')
const statusFilter = ref<TaskStatus | ''>('')
const PAGE_SIZE = 20
const page = ref(0) // 0 基

const selectedTaskId = ref<string | null>(null)
const selectedEvents = ref<TaskEventItem[]>([])
const eventsLoading = ref(false)

const totalPages = computed(() => Math.max(1, Math.ceil(total.value / PAGE_SIZE)))

const kindText: Record<TaskKind, string> = {
  compare: '合同比对',
  raw: '无标注比对',
  statement: '对帐单统计',
}

const statusText: Record<TaskStatus, string> = {
  pending: '排队中',
  running: '处理中',
  done: '已完成',
  failed: '失败',
}

function riskText(risk: OverallRisk | null): string {
  switch (risk) {
    case 'high': return '高风险'
    case 'medium': return '中风险'
    case 'low': return '低风险'
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

async function refresh(): Promise<void> {
  loading.value = true
  loadError.value = null
  try {
    const resp = await listTasks({
      kind: kindFilter.value || undefined,
      status: statusFilter.value || undefined,
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
  selectedTaskId.value = null
  selectedEvents.value = []
  await refresh()
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

async function showEvents(taskId: string): Promise<void> {
  if (selectedTaskId.value === taskId) {
    // 折叠
    selectedTaskId.value = null
    selectedEvents.value = []
    return
  }
  selectedTaskId.value = taskId
  selectedEvents.value = []
  eventsLoading.value = true
  try {
    const resp = await getTaskEvents(taskId)
    selectedEvents.value = resp.items
  } catch (e) {
    selectedEvents.value = []
    // 不致命,记录到控制台
    console.warn('[history] load events failed:', e)
  } finally {
    eventsLoading.value = false
  }
}

onMounted(refresh)
</script>

<template>
  <div class="history">
    <section class="card">
      <div class="head-row">
        <h2 class="page-title">比对记录</h2>
        <button class="btn" :disabled="loading" @click="refresh">
          {{ loading ? '刷新中…' : '刷新' }}
        </button>
      </div>
      <p class="muted page-desc">查看历史比对与统计任务,点击「查看」可重新打开对应报告页。</p>

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
        没有符合条件的记录。
      </div>

      <div v-else class="table-wrap">
        <table class="task-table">
          <thead>
            <tr>
              <th>创建时间</th>
              <th>类型</th>
              <th>文件</th>
              <th>状态</th>
              <th>结果</th>
              <th>耗时</th>
              <th class="col-actions">操作</th>
            </tr>
          </thead>
          <tbody>
            <template v-for="item in items" :key="item.task_id">
              <tr>
                <td class="mono small">{{ formatTime(item.created_at) }}</td>
                <td>
                  <span class="kind-tag" :data-kind="item.kind">{{ kindText[item.kind] }}</span>
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
                <td>{{ formatElapsed(item.elapsed) }}</td>
                <td class="col-actions">
                  <button class="chip" type="button" @click="viewReport(item)">查看</button>
                  <button
                    class="chip"
                    type="button"
                    :disabled="item.status !== 'done' && item.status !== 'failed'"
                    @click="showEvents(item.task_id)"
                  >
                    {{ selectedTaskId === item.task_id ? '收起' : '时间线' }}
                  </button>
                </td>
              </tr>
              <tr v-if="selectedTaskId === item.task_id" class="event-row">
                <td colspan="7">
                  <div v-if="eventsLoading" class="muted">加载时间线…</div>
                  <ol v-else-if="selectedEvents.length" class="event-list">
                    <li v-for="ev in selectedEvents" :key="ev.id">
                      <span class="ev-time mono small">{{ formatTime(ev.created_at) }}</span>
                      <span class="ev-stage">{{ ev.stage }}</span>
                      <span class="muted small">
                        progress {{ (ev.progress * 100).toFixed(0) }}%
                      </span>
                    </li>
                  </ol>
                  <p v-else class="muted">该任务没有已保存的里程碑事件(可能创建于本次持久化功能上线前)。</p>
                  <p v-if="item.error" class="err small">错误: {{ item.error }}</p>
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
.head-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 4px;
}
.page-title {
  margin: 0;
  font-size: 18px;
}
.page-desc {
  margin: 0 0 14px;
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
  padding: 30px 0;
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
}
.task-table tbody tr:hover {
  background: var(--surface-2);
}
.task-table .col-actions {
  white-space: nowrap;
  text-align: right;
}
.task-table .col-actions .chip {
  margin-left: 6px;
}
.file-cell {
  max-width: 320px;
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

.risk-tag { color: var(--text-muted); }
.risk-tag.risk-high { color: var(--risk-high); font-weight: 600; }
.risk-tag.risk-medium { color: var(--risk-medium); font-weight: 600; }
.risk-tag.risk-low { color: var(--risk-low); }
.risk-tag.risk-clean { color: var(--risk-clean); }

.event-row td {
  background: var(--surface-2);
  padding: 12px 16px;
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
</style>
