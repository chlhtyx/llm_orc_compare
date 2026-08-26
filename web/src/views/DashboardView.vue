<script setup lang="ts">
// 运行看板:按天(北京时间)统计任务数、模型 / OCR 调用数与外部接口调用数。
// 数据来自 GET /api/v1/stats/daily(后端已按天补零、升序返回);柱状图为纯
// CSS 手绘,不引入图表库,与项目零 UI 依赖的约定一致。
import { computed, onMounted, ref } from 'vue'
import { getDailyStats, type DailyStatsPoint, type DailyStatsResponse } from '@/api/stats'

type Metric = 'tasks' | 'llm' | 'ext'

const RANGE_OPTIONS = [7, 14, 30, 90] as const
/** 与后端 repository.daily_stats 的跨度上限一致。 */
const MAX_RANGE_DAYS = 366
const metricOptions: { key: Metric; label: string }[] = [
  { key: 'tasks', label: '任务数' },
  { key: 'llm', label: '模型调用' },
  { key: 'ext', label: '外部调用' },
]

/** 浏览器本地日期 → YYYY-MM-DD(时区差异由后端按北京时间截断兜底)。 */
function localDateStr(d: Date): string {
  const m = String(d.getMonth() + 1).padStart(2, '0')
  const day = String(d.getDate()).padStart(2, '0')
  return `${d.getFullYear()}-${m}-${day}`
}
function addDays(d: Date, n: number): Date {
  const c = new Date(d)
  c.setDate(c.getDate() + n)
  return c
}

/** 预设片与日期选择器统一落到 start/end 区间,单一请求路径。 */
const rangeStart = ref(localDateStr(addDays(new Date(), -13)))
const rangeEnd = ref(localDateStr(new Date()))
/** 当前命中的预设天数;手动改日期后置 0(无高亮)。 */
const presetDays = ref<number>(14)
const rangeError = ref('')
const metric = ref<Metric>('tasks')
const data = ref<DailyStatsResponse | null>(null)
const loading = ref(false)
const error = ref('')

const series = computed<DailyStatsPoint[]>(() => data.value?.series ?? [])

async function load(): Promise<void> {
  loading.value = true
  error.value = ''
  try {
    data.value = await getDailyStats({ start: rangeStart.value, end: rangeEnd.value })
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  } finally {
    loading.value = false
  }
}
onMounted(load)

function setDays(n: number): void {
  if (presetDays.value === n) return
  presetDays.value = n
  rangeError.value = ''
  rangeEnd.value = localDateStr(new Date())
  rangeStart.value = localDateStr(addDays(new Date(), -(n - 1)))
  load()
}

/** 手动改日期:本地校验后请求;预设高亮取消。 */
function onRangeChange(): void {
  presetDays.value = 0
  if (!rangeStart.value || !rangeEnd.value) return
  if (rangeStart.value > rangeEnd.value) {
    rangeError.value = '起始日期不能晚于结束日期'
    return
  }
  const span =
    Math.round(
      (new Date(rangeEnd.value).getTime() - new Date(rangeStart.value).getTime()) / 86400000,
    ) + 1
  if (span > MAX_RANGE_DAYS) {
    rangeError.value = `区间跨度不能超过 ${MAX_RANGE_DAYS} 天`
    return
  }
  rangeError.value = ''
  load()
}

// —— 汇总卡片 ——

const summary = computed(() => {
  let tasks = 0
  let tasksDone = 0
  let tasksFailed = 0
  let llm = 0
  let llmFailed = 0
  let ext = 0
  let extFailed = 0
  for (const p of series.value) {
    tasks += p.tasks.total
    tasksDone += p.tasks.by_status.done ?? 0
    tasksFailed += p.tasks.by_status.failed ?? 0
    llm += p.llm_calls.total
    llmFailed += p.llm_calls.failed
    ext += p.external_calls.total
    extFailed += p.external_calls.failed
  }
  return { tasks, tasksDone, tasksFailed, llm, llmFailed, ext, extFailed }
})

const avgTasks = computed(() =>
  (summary.value.tasks / (series.value.length || 1)).toFixed(1),
)

/** 最后一格即北京时间今天。 */
const today = computed<DailyStatsPoint | null>(() =>
  series.value.length ? series.value[series.value.length - 1] : null,
)

const hasAnyData = computed(() =>
  summary.value.tasks > 0 || summary.value.llm > 0 || summary.value.ext > 0,
)

// —— 每日趋势柱状图 ——

interface DaySeg {
  date: string
  ok: number
  fail: number
  /** 仅任务指标:pending / running 的中性段 */
  other: number
  total: number
  point: DailyStatsPoint
}

const chartData = computed<DaySeg[]>(() =>
  series.value.map((p) => {
    if (metric.value === 'tasks') {
      const done = p.tasks.by_status.done ?? 0
      const failed = p.tasks.by_status.failed ?? 0
      return {
        date: p.date, ok: done, fail: failed,
        other: p.tasks.total - done - failed,
        total: p.tasks.total, point: p,
      }
    }
    if (metric.value === 'llm') {
      const { total, success, failed } = p.llm_calls
      return { date: p.date, ok: success, fail: failed, other: 0, total, point: p }
    }
    const ext = p.external_calls
    return { date: p.date, ok: ext.success, fail: ext.failed, other: 0, total: ext.total, point: p }
  }),
)

const maxTotal = computed(() => Math.max(1, ...chartData.value.map((d) => d.total)))

function pct(v: number): string {
  return `${(v / maxTotal.value) * 100}%`
}

const axisStep = computed(() => Math.max(1, Math.ceil(chartData.value.length / 14)))
function showLabel(i: number): boolean {
  const last = chartData.value.length - 1
  return i % axisStep.value === 0 || i === last
}

function shortDate(d: string): string {
  return d.slice(5) // MM-DD
}

function tooltip(d: DaySeg): string {
  const p = d.point
  return [
    p.date,
    `任务 ${p.tasks.total}(完成 ${p.tasks.by_status.done ?? 0} / 失败 ${p.tasks.by_status.failed ?? 0})`,
    `模型调用 ${p.llm_calls.total}(成功 ${p.llm_calls.success} / 失败 ${p.llm_calls.failed})`,
    `外部调用 ${p.external_calls.total}(成功 ${p.external_calls.success} / 失败 ${p.external_calls.failed})`,
  ].join('\n')
}

// —— 类型分布(区间累计)——

interface BreakdownItem {
  key: string
  count: number
}

function aggregate(maps: Record<string, number>[]): BreakdownItem[] {
  const acc = new Map<string, number>()
  for (const m of maps) {
    for (const [k, v] of Object.entries(m)) {
      acc.set(k, (acc.get(k) ?? 0) + v)
    }
  }
  return [...acc.entries()]
    .map(([key, count]) => ({ key, count }))
    .sort((a, b) => b.count - a.count)
}

const taskKindBreakdown = computed(() => aggregate(series.value.map((p) => p.tasks.by_kind)))
const llmKindBreakdown = computed(() => aggregate(series.value.map((p) => p.llm_calls.by_kind)))
const endpointBreakdown = computed(() =>
  aggregate(series.value.map((p) => p.external_calls.by_endpoint)),
)

const taskKindText: Record<string, string> = {
  compare: '合同比对',
  raw: '无标注比对',
  statement: '金额统计',
}

/** 与 LlmCallList.vue 的 kind 标签保持一致。 */
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

const endpointText: Record<string, string> = {
  'contractCompare.submit': '提交比对',
  'contractCompare.result': '结果查询',
  'contractCompare.image': '图片获取',
  'contractCompare.report': '报告下载',
}

// —— 每日明细表(最新在前)——

const tableRows = computed(() => [...series.value].reverse())

function inProgress(p: DailyStatsPoint): number {
  return p.tasks.total - (p.tasks.by_status.done ?? 0) - (p.tasks.by_status.failed ?? 0)
}
</script>

<template>
  <div>
    <div class="card dash-head">
      <div>
        <h2 class="dash-title">运行看板</h2>
        <p v-if="data" class="muted dash-range">
          统计区间 {{ data.start }} ~ {{ data.end }}(北京时间,共 {{ data.days }} 天)
        </p>
        <p v-if="rangeError" class="err range-error">{{ rangeError }}</p>
      </div>
      <div class="dash-controls">
        <div class="chip-row">
          <button
            v-for="n in RANGE_OPTIONS"
            :key="n"
            class="chip"
            :class="{ on: presetDays === n }"
            @click="setDays(n)"
          >
            近 {{ n }} 天
          </button>
        </div>
        <div class="range-row">
          <input
            v-model="rangeStart"
            type="date"
            class="input range-input"
            :disabled="loading"
            aria-label="起始日期"
            @change="onRangeChange"
          />
          <span class="muted range-sep">至</span>
          <input
            v-model="rangeEnd"
            type="date"
            class="input range-input"
            :disabled="loading"
            aria-label="结束日期"
            @change="onRangeChange"
          />
          <button class="btn" :disabled="loading" @click="load">刷新</button>
        </div>
      </div>
    </div>

    <div v-if="error" class="card dash-section">
      <p class="err">统计加载失败:{{ error }}</p>
      <button class="btn" @click="load">重试</button>
    </div>

    <div v-else-if="loading && !data" class="card dash-section muted">加载中…</div>

    <template v-else-if="data">
      <div class="sum-grid">
        <div class="card sum-card">
          <span class="sum-label">任务总数</span>
          <span class="sum-num">{{ summary.tasks }}</span>
          <span class="sum-sub">
            完成 {{ summary.tasksDone }} · 失败
            <span :class="{ 'sum-fail': summary.tasksFailed > 0 }">{{ summary.tasksFailed }}</span>
            · 日均 {{ avgTasks }}
          </span>
        </div>
        <div class="card sum-card">
          <span class="sum-label">模型 / OCR 调用</span>
          <span class="sum-num">{{ summary.llm }}</span>
          <span class="sum-sub">
            成功 {{ summary.llm - summary.llmFailed }} · 失败
            <span :class="{ 'sum-fail': summary.llmFailed > 0 }">{{ summary.llmFailed }}</span>
          </span>
        </div>
        <div class="card sum-card">
          <span class="sum-label">外部接口调用</span>
          <span class="sum-num">{{ summary.ext }}</span>
          <span class="sum-sub">
            成功 {{ summary.ext - summary.extFailed }} · 失败
            <span :class="{ 'sum-fail': summary.extFailed > 0 }">{{ summary.extFailed }}</span>
          </span>
        </div>
        <div class="card sum-card">
          <span class="sum-label">今日(北京时间)</span>
          <span class="sum-num">{{ today?.tasks.total ?? 0 }}</span>
          <span class="sum-sub">
            模型调用 {{ today?.llm_calls.total ?? 0 }} · 外部调用 {{ today?.external_calls.total ?? 0 }}
          </span>
        </div>
      </div>

      <div class="card dash-section">
        <div class="chart-head">
          <h3 class="section-title chart-title">每日趋势</h3>
          <div class="chip-row">
            <button
              v-for="m in metricOptions"
              :key="m.key"
              class="chip"
              :class="{ on: metric === m.key }"
              @click="metric = m.key"
            >
              {{ m.label }}
            </button>
          </div>
        </div>
        <p v-if="!hasAnyData" class="muted">该区间暂无任何调用数据。</p>
        <template v-else>
          <div class="legend muted">
            <span><i class="lg lg-ok" />成功</span>
            <span><i class="lg lg-fail" />失败</span>
            <span v-if="metric === 'tasks'"><i class="lg lg-other" />进行中 / 排队</span>
            <span class="legend-max">峰值 {{ maxTotal }}</span>
          </div>
          <div class="chart-scroll">
            <div class="chart-plot">
              <div v-for="(d, i) in chartData" :key="d.date" class="chart-col" :title="tooltip(d)">
                <div class="bar-stack">
                  <div class="seg seg-ok" :style="{ height: pct(d.ok) }" />
                  <div class="seg seg-fail" :style="{ height: pct(d.fail) }" />
                  <div v-if="d.other > 0" class="seg seg-other" :style="{ height: pct(d.other) }" />
                </div>
                <span class="col-date">{{ showLabel(i) ? shortDate(d.date) : '' }}</span>
              </div>
            </div>
          </div>
        </template>
      </div>

      <div class="card dash-section">
        <h3 class="section-title">类型分布(区间累计)</h3>
        <div class="bd-row">
          <span class="bd-label">任务类型</span>
          <span v-if="!taskKindBreakdown.length" class="muted">无</span>
          <span v-for="b in taskKindBreakdown" :key="b.key" class="bd-chip">
            {{ taskKindText[b.key] ?? b.key }} {{ b.count }}
          </span>
        </div>
        <div class="bd-row">
          <span class="bd-label">模型调用</span>
          <span v-if="!llmKindBreakdown.length" class="muted">无</span>
          <span v-for="b in llmKindBreakdown" :key="b.key" class="bd-chip">
            {{ llmKindText[b.key] ?? b.key }} {{ b.count }}
          </span>
        </div>
        <div class="bd-row">
          <span class="bd-label">外部端点</span>
          <span v-if="!endpointBreakdown.length" class="muted">无</span>
          <span v-for="b in endpointBreakdown" :key="b.key" class="bd-chip">
            {{ endpointText[b.key] ?? b.key }} {{ b.count }}
          </span>
        </div>
      </div>

      <div class="card dash-section">
        <h3 class="section-title">每日明细</h3>
        <div class="tbl-scroll">
          <table class="tbl">
            <thead>
              <tr>
                <th>日期</th>
                <th>任务</th>
                <th>模型 / OCR 调用</th>
                <th>外部接口调用</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="p in tableRows" :key="p.date">
                <td class="mono">{{ p.date }}</td>
                <td>
                  {{ p.tasks.total }}
                  <span class="muted">
                    (完成 {{ p.tasks.by_status.done ?? 0 }} / 失败 {{ p.tasks.by_status.failed ?? 0 }}<span
                      v-if="inProgress(p) > 0"
                    > / 进行中 {{ inProgress(p) }}</span>)
                  </span>
                </td>
                <td>
                  {{ p.llm_calls.total }}
                  <span class="muted">(成功 {{ p.llm_calls.success }} / 失败 {{ p.llm_calls.failed }})</span>
                </td>
                <td>
                  {{ p.external_calls.total }}
                  <span class="muted">
                    (成功 {{ p.external_calls.success }} / 失败 {{ p.external_calls.failed }})
                  </span>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>
    </template>
  </div>
</template>

<style scoped>
.dash-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  flex-wrap: wrap;
}
.dash-title {
  margin: 0;
  font-size: 18px;
}
.dash-range {
  margin: 4px 0 0;
  font-size: 13px;
}
.dash-controls {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}
.range-row {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
/* 覆盖全局 .input 的 width:100%,日期控件随内容自适应 */
.range-input {
  width: auto;
  padding: 6px 8px;
  font-size: 13px;
}
.range-sep {
  font-size: 13px;
}
.range-error {
  margin: 6px 0 0;
  font-size: 13px;
}
.dash-section {
  margin-top: 16px;
}
.err {
  color: var(--risk-high);
  margin: 0 0 10px;
}

/* 汇总卡片 */
.sum-grid {
  margin-top: 16px;
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 16px;
}
.sum-grid .card + .card {
  margin-top: 0; /* 覆盖全局 .card + .card 的 16px,网格间距由 gap 提供 */
}
.sum-card {
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: 16px 20px;
}
.sum-label {
  font-size: 12px;
  color: var(--text-muted);
}
.sum-num {
  font-size: 28px;
  font-weight: 700;
  line-height: 1.2;
}
.sum-sub {
  font-size: 12px;
  color: var(--text-muted);
}
.sum-fail {
  color: var(--risk-high);
  font-weight: 600;
}
@media (max-width: 900px) {
  .sum-grid {
    grid-template-columns: repeat(2, 1fr);
  }
}
@media (max-width: 520px) {
  .sum-grid {
    grid-template-columns: 1fr;
  }
}

/* 趋势图 */
.chart-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
}
.chart-title {
  margin: 0;
}
.chip-row {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
}
.legend {
  display: flex;
  align-items: center;
  gap: 14px;
  font-size: 12px;
  margin: 8px 0 6px;
}
.legend-max {
  margin-left: auto;
}
.lg {
  display: inline-block;
  width: 10px;
  height: 10px;
  border-radius: 2px;
  margin-right: 4px;
  vertical-align: -1px;
}
.lg-ok {
  background: var(--primary);
}
.lg-fail {
  background: var(--risk-high);
}
.lg-other {
  background: #9aa4b2;
}
.chart-scroll {
  overflow-x: auto;
}
.chart-plot {
  display: flex;
  gap: 3px;
  min-width: 420px;
  height: 180px;
  align-items: stretch;
}
.chart-col {
  flex: 1 1 0;
  min-width: 8px;
  display: flex;
  flex-direction: column;
}
.bar-stack {
  flex: 1;
  display: flex;
  flex-direction: column-reverse; /* ok 段沉底,失败 / 进行中向上堆叠 */
}
.seg {
  width: 100%;
}
.seg-ok {
  background: var(--primary);
}
.seg-fail {
  background: var(--risk-high);
}
.seg-other {
  background: #9aa4b2;
}
.col-date {
  height: 16px;
  margin-top: 4px;
  font-size: 10px;
  color: var(--text-muted);
  text-align: center;
  white-space: nowrap;
}

/* 类型分布 */
.bd-row {
  display: flex;
  align-items: baseline;
  gap: 8px;
  flex-wrap: wrap;
  margin-top: 8px;
}
.bd-label {
  flex: 0 0 64px;
  font-size: 13px;
  font-weight: 500;
  color: var(--text-muted);
}
.bd-chip {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  background: var(--surface-2);
  border-radius: 999px;
  padding: 2px 10px;
  font-size: 12px;
}

/* 明细表 */
.tbl-scroll {
  overflow-x: auto;
}
</style>
