<script setup lang="ts">
import { computed, onBeforeUnmount, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ApiError } from '@/api/statement'
import { useStatementTaskStore } from '@/stores/statementTask'
import type {
  ChangeVerdict,
  StatementSummaryReport,
} from '@/api/types'

const props = defineProps<{ taskId: string }>()
const router = useRouter()
const store = useStatementTaskStore()

const loadError = ref<string | null>(null)

async function attach(): Promise<void> {
  if (store.taskId === props.taskId && store.status !== 'failed') return
  try {
    await store.load(props.taskId)
  } catch (e) {
    loadError.value = e instanceof ApiError ? e.message : `加载失败: ${(e as Error).message}`
  }
}

void attach()

const report = computed<StatementSummaryReport | null>(() => store.report)

onBeforeUnmount(() => {
  store.dispose()
})

// —— 进度展示(statement stage 是动态名,做轻量映射)——
const pct = computed(() => Math.min(100, Math.round((store.progress ?? 0) * 100)))
const stageText = computed(() => {
  const s = store.stage
  if (!s) return store.status === 'pending' ? '排队中' : '处理中…'
  if (s === 'statement_start') return '开始处理'
  if (s === 'statement_aggregate') return '汇总金额'
  if (s === 'done') return '完成'
  if (s === 'failed') return '处理失败'
  if (s.startsWith('statement_file_')) {
    // statement_file_0 / statement_file_0_done
    const m = s.match(/^statement_file_(\d+)/)
    const idx = m ? Number(m[1]) + 1 : ''
    return s.endsWith('_done') ? `第 ${idx} 个文件完成` : `处理第 ${idx} 个文件`
  }
  return s
})

const liveElapsed = ref(0)
const mountTime = Date.now()
let timer: ReturnType<typeof setInterval> | null = null
function ensureTimer(): void {
  if ((store.status === 'pending' || store.status === 'running') && !timer) {
    timer = setInterval(() => {
      liveElapsed.value = (Date.now() - mountTime) / 1000
    }, 200)
  } else if (store.status === 'done' || store.status === 'failed') {
    if (timer) {
      clearInterval(timer)
      timer = null
    }
  }
}
ensureTimer()
const elapsedSec = computed(() =>
  store.status === 'done' || store.status === 'failed'
    ? (store.elapsed ?? 0)
    : liveElapsed.value,
)

// —— 报告展示 ——
function fmtMoney(v: number): string {
  return v.toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
}
function fmtElapsed(sec: number): string {
  if (sec <= 0) return '0s'
  if (sec < 60) return `${sec.toFixed(1)}s`
  const m = Math.floor(sec / 60)
  const s = Math.round(sec % 60)
  return `${m}m${s}s`
}

const verdictText: Record<ChangeVerdict, string> = {
  clean: '一致',
  changed: '声明不一致',
  needs_review: '待复核',
}

const verdictClass = computed(() => {
  if (!report.value) return ''
  return `verdict-${report.value.verdict}`
})

const columnTotalsRows = computed(() => {
  if (!report.value) return []
  return Object.entries(report.value.grand_totals_by_column)
    .map(([col, val]) => ({ col, val }))
    .sort((a, b) => b.val - a.val)
})

function tableSourceBadge(source: string | undefined): string {
  if (source === 'heuristic') return '启发式'
  if (source === 'llm') return 'LLM 指认'
  if (source === 'none') return '定位失败'
  return source ?? ''
}
</script>

<template>
  <div class="report">
    <header class="card report-head">
      <div class="head-left">
        <h2 class="page-title">
          对帐单金额统计
          <span class="task-id muted">task {{ taskId }}</span>
        </h2>
        <template v-if="!report">
          <div class="tracker-head">
            <span class="stage">{{ stageText }}</span>
            <span class="pct muted">{{ pct }}%</span>
          </div>
          <div class="progress"><span :style="{ width: pct + '%' }" /></div>
          <div class="tracker-foot">
            <span class="elapsed">总耗时 {{ fmtElapsed(elapsedSec) }}</span>
            <span v-if="store.status === 'failed'" class="err">
              失败:{{ store.error || '未知错误' }}
            </span>
            <span v-else class="muted">处理中…</span>
          </div>
        </template>
      </div>
      <div class="head-actions">
        <button class="btn" type="button" @click="router.push('/statement')">新建统计</button>
      </div>
    </header>

    <p v-if="loadError" class="card err">{{ loadError }}</p>

    <template v-if="report">
      <!-- verdict 提示条 -->
      <section
        v-if="report.verdict !== 'clean'"
        class="card verdict-banner"
        :class="verdictClass"
      >
        <strong>{{ verdictText[report.verdict] }}</strong>
        <ul v-if="report.reasons.length">
          <li v-for="(r, i) in report.reasons" :key="i">{{ r }}</li>
        </ul>
      </section>

      <!-- 总合计大字 -->
      <section class="card hero">
        <span class="hero-label">总合计(所有文件)</span>
        <span class="hero-num">¥{{ fmtMoney(report.grand_total) }}</span>
        <span class="hero-meta muted">
          {{ report.total_files }} 个文件 · {{ report.total_tables }} 张表 · {{ report.total_items }} 条金额
        </span>
      </section>

      <!-- 按列汇总 + 列定位分布 -->
      <section class="card">
        <h3 class="section-title">按金额列汇总</h3>
        <table v-if="columnTotalsRows.length" class="tbl">
          <thead>
            <tr><th>列名</th><th class="num-col">合计(元)</th></tr>
          </thead>
          <tbody>
            <tr v-for="row in columnTotalsRows" :key="row.col">
              <td>{{ row.col }}</td>
              <td class="num">{{ fmtMoney(row.val) }}</td>
            </tr>
          </tbody>
        </table>
        <p v-else class="muted">未识别到任何金额数据。</p>

        <div class="col-detection">
          <span class="muted">列定位分布:</span>
          <span class="badge badge-heuristic">启发式 {{ report.column_detection_summary.heuristic ?? 0 }}</span>
          <span class="badge badge-llm">LLM {{ report.column_detection_summary.llm ?? 0 }}</span>
          <span class="badge badge-none">失败 {{ report.column_detection_summary.none ?? 0 }}</span>
        </div>
      </section>

      <!-- 每个文件展开 -->
      <section
        v-for="f in report.files"
        :key="f.file_index"
        class="card file-card"
        :class="{ 'file-error': !!f.error }"
      >
        <header class="file-head">
          <h3 class="file-title">
            {{ f.file_name || `文件 ${f.file_index + 1}` }}
            <span class="muted file-idx">#{{ f.file_index + 1 }}</span>
          </h3>
          <div class="file-meta">
            <span class="file-total">合计 ¥{{ fmtMoney(f.total_amount) }}</span>
            <span v-if="f.error" class="badge badge-none">失败</span>
            <span
              v-else-if="f.recognition_status === 'needs_review'"
              class="badge badge-none"
            >OCR 待复核</span>
            <span v-else class="badge badge-heuristic">识别可靠</span>
          </div>
        </header>

        <p v-if="f.error" class="err file-err">处理失败:{{ f.error }}</p>

        <template v-if="!f.error">
          <div
            v-for="t in f.tables"
            :key="t.table_index"
            class="table-block"
          >
            <div class="table-head">
              <span>表 {{ t.table_index + 1 }}(第 {{ t.page_index + 1 }} 页)</span>
              <span class="muted">{{ t.items.length }} 条金额</span>
            </div>

            <!-- 声明核对 -->
            <div v-if="Object.keys(t.totals_match).length" class="verify-row">
              <span class="muted">声明合计核对:</span>
              <span
                v-for="(ok, col) in t.totals_match"
                :key="col"
                class="verify-item"
                :class="{ ok, bad: !ok }"
              >
                {{ col }}: 实算 ¥{{ fmtMoney(t.column_sums[col] ?? 0) }}
                / 声明 ¥{{ fmtMoney(t.declared_totals[col] ?? 0) }}
                ({{ ok ? '一致' : '不一致' }})
              </span>
            </div>

            <!-- 明细表 -->
            <table v-if="t.items.length" class="tbl">
              <thead>
                <tr>
                  <th class="row-idx">行</th>
                  <th>标识</th>
                  <th>金额列</th>
                  <th>OCR 原文</th>
                  <th class="num-col">金额(元)</th>
                  <th class="src-col">定位</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="(it, i) in t.items" :key="i">
                  <td class="muted">{{ it.row_index + 1 }}</td>
                  <td>{{ it.row_label || '—' }}</td>
                  <td>{{ it.column }}</td>
                  <td><code>{{ it.raw_cell }}</code></td>
                  <td class="num">{{ fmtMoney(it.value) }}</td>
                  <td>
                    <span
                      class="badge-sm"
                      :class="`badge-${t.column_source[it.column] ?? 'none'}`"
                    >{{ tableSourceBadge(t.column_source[it.column]) }}</span>
                  </td>
                </tr>
              </tbody>
            </table>
            <p v-else class="muted">本表未识别到金额行(可能列定位失败或全部为合计行)。</p>
          </div>

          <p v-if="!f.tables.length" class="muted">未识别到结构化表格。</p>
        </template>
      </section>
    </template>

    <section v-else-if="!loadError" class="card">
      <p class="muted">等待报告生成…</p>
    </section>
  </div>
</template>

<style scoped>
.report-head {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 16px;
}
.page-title {
  margin: 0 0 10px;
  font-size: 18px;
}
.task-id {
  font-size: 12px;
  font-weight: 400;
  margin-left: 6px;
}
.tracker-head {
  display: flex;
  justify-content: space-between;
  margin-bottom: 6px;
}
.stage {
  font-weight: 600;
}
.progress {
  width: 100%;
  height: 6px;
  background: var(--surface-2);
  border-radius: 3px;
  overflow: hidden;
}
.progress span {
  display: block;
  height: 100%;
  background: var(--primary);
  transition: width 0.2s;
}
.tracker-foot {
  margin-top: 8px;
  font-size: 13px;
}
.elapsed {
  margin-right: 12px;
  color: var(--text-muted);
  font-variant-numeric: tabular-nums;
}
.err {
  color: var(--risk-high);
}

/* 总合计 */
.hero {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 4px;
}
.hero-label {
  font-size: 13px;
  color: var(--text-muted);
}
.hero-num {
  font-size: 36px;
  font-weight: 700;
  color: var(--primary);
  font-variant-numeric: tabular-nums;
  line-height: 1.1;
}
.hero-meta {
  font-size: 13px;
}

/* verdict banner */
.verdict-banner {
  border-left: 4px solid var(--risk-medium);
  background: var(--risk-medium-bg);
}
.verdict-banner.verdict-changed {
  border-left-color: var(--risk-high);
  background: var(--risk-high-bg, var(--risk-medium-bg));
}
.verdict-banner ul {
  margin: 6px 0 0;
  padding-left: 18px;
}
.verdict-banner li {
  font-size: 13px;
  margin: 2px 0;
}

/* 表格通用 */
.tbl {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
  margin-top: 6px;
}
.tbl th,
.tbl td {
  text-align: left;
  padding: 6px 8px;
  border-bottom: 1px solid var(--border);
  vertical-align: top;
}
.tbl th {
  font-weight: 600;
  color: var(--text-muted);
  font-size: 12px;
  background: var(--surface-2);
}
.num-col,
.num {
  text-align: right;
  font-variant-numeric: tabular-nums;
}
.row-idx {
  width: 40px;
}
.src-col {
  width: 80px;
}
code {
  font-family: var(--mono);
  font-size: 12px;
  background: var(--surface-2);
  padding: 1px 4px;
  border-radius: 3px;
}

/* 列定位徽章 */
.col-detection {
  margin-top: 12px;
  display: flex;
  gap: 8px;
  align-items: center;
  flex-wrap: wrap;
  font-size: 12px;
}
.badge,
.badge-sm {
  display: inline-block;
  padding: 2px 8px;
  border-radius: 10px;
  font-size: 12px;
  font-weight: 500;
  border: 1px solid var(--border);
  background: var(--surface-2);
}
.badge-sm {
  padding: 1px 6px;
  font-size: 11px;
}
.badge-heuristic,
.badge-sm.badge-heuristic {
  background: var(--risk-clean-bg, var(--surface-2));
  color: var(--risk-clean);
}
.badge-llm,
.badge-sm.badge-llm {
  background: var(--primary-bg, var(--surface-2));
  color: var(--primary);
}
.badge-none,
.badge-sm.badge-none {
  background: var(--risk-high-bg, var(--surface-2));
  color: var(--risk-high);
}

/* 文件卡 */
.file-card {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.file-card.file-error {
  border-left: 4px solid var(--risk-high);
}
.file-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}
.file-title {
  margin: 0;
  font-size: 15px;
  font-weight: 600;
}
.file-idx {
  font-size: 12px;
  font-weight: 400;
}
.file-meta {
  display: flex;
  gap: 10px;
  align-items: center;
}
.file-total {
  font-weight: 700;
  font-variant-numeric: tabular-nums;
  color: var(--primary);
}
.file-err {
  margin: 0;
  font-size: 13px;
}

/* 表块 */
.table-block {
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  padding: 10px 12px;
  background: var(--surface);
}
.table-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 6px;
  font-weight: 600;
  font-size: 13px;
}
.verify-row {
  display: flex;
  flex-wrap: wrap;
  gap: 6px 14px;
  margin: 6px 0;
  font-size: 12px;
  padding: 6px 8px;
  background: var(--surface-2);
  border-radius: 4px;
}
.verify-item.ok {
  color: var(--risk-clean);
}
.verify-item.bad {
  color: var(--risk-high);
  font-weight: 600;
}
</style>
