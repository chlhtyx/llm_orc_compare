<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import ProgressTracker from '@/components/ProgressTracker.vue'
import DiffList from '@/components/DiffList.vue'
import KeyElementTable from '@/components/KeyElementTable.vue'
import PdfViewer from '@/components/PdfViewer.vue'
import { ApiError, getOriginalPdfUrl, getSourcePdfUrl } from '@/api/compare'
import { useTaskStore } from '@/stores/task'
import { useReportStore } from '@/stores/report'
import type { Diff, TamperReport } from '@/api/types'

const props = defineProps<{ taskId: string }>()
const router = useRouter()
const taskStore = useTaskStore()
const reportStore = useReportStore()

const loadError = ref<string | null>(null)

// 进入报告页时接管任务:若 store 已是当前任务(submit 后跳转)则不重复加载。
async function attach(): Promise<void> {
if (taskStore.taskId === props.taskId && taskStore.status !== 'failed') return
try {
    await taskStore.load(props.taskId)
} catch (e) {
    loadError.value = e instanceof ApiError ? e.message : `加载失败: ${(e as Error).message}`
}
}

void attach()

// 任务终结且带 report 时,同步到 report store。
watch(
() => taskStore.report,
(r: TamperReport | null) => {
    reportStore.set(r)
},
{ immediate: true },
)

onBeforeUnmount(() => {
// 离开页面时停止 SSE/轮询,但保留状态以便返回查看
taskStore.dispose()
})

const showPdf = ref(false)
const showSourcePdf = ref(false)
const sourceScrollProgress = ref(0)
const targetScrollProgress = ref(0)
const filter = ref<'all' | 'risk' | 'modified'>('all')
const selectedClauseId = ref<string | null>(null)

const changeStatusText = computed(() => ({
  clean: '未发现内容变化',
  changed: '发现确认内容变化',
  needs_review: '存在待人工复核内容',
}[reportStore.report?.change_status ?? 'clean']))

const visibleDiffs = computed<Diff[]>(() => {
const list = reportStore.diffsBySeverity
if (filter.value === 'risk') return list.filter((d) => d.risk_level !== 'none')
if (filter.value === 'modified') return list.filter((d) => d.status === 'modified')
return list
})

// 是否存在任何带风险等级的 diff(关闭风险判别时所有 diff.risk_level 都是 'none',
// 此时隐藏「有风险」筛选 chip;开启风险时若有非 none 风险才显示)。
const hasRiskDiffs = computed<boolean>(() =>
reportStore.diffs.some((d) => d.risk_level !== 'none'),
)
// 高风险要素校验区:仅在报告里实际抽取到要素时才渲染
// (关闭风险判别时 key_elements 为空,该区自动隐藏)。
const hasKeyElements = computed<boolean>(() => reportStore.keyElements.length > 0)
const hasSourcePdfPreview = computed<boolean>(() =>
  reportStore.report?.source_annotation_status !== 'unavailable',
)
const hasBidirectionalPdfPreview = computed<boolean>(() =>
  hasSourcePdfPreview.value
  && reportStore.hasPdfHighlights,
)

const pdfUrl = computed(() => {
return getSourcePdfUrl(props.taskId)
})
const originalPdfUrl = computed(() => getOriginalPdfUrl(props.taskId))

function syncSourcePdfScroll(progress: number) {
  targetScrollProgress.value = progress
}

function syncTargetPdfScroll(progress: number) {
  sourceScrollProgress.value = progress
}

/** 点击条款 → 选中 + 展开 PDF 预览 + 滚动到对应页 */
function onSelectClause(id: string) {
  selectedClauseId.value = id
  if (!showPdf.value) {
    showPdf.value = true
  }
  if (reportStore.hasSourcePdfHighlights) {
    showSourcePdf.value = true
  }
}
</script>

<template>
<div class="report">
    <header class="card report-head">
        <div class="head-left">
        <h2 class="page-title">
        比对报告
        <span class="task-id muted">task {{ taskId }}</span>
        </h2>
        <ProgressTracker
        :status="taskStore.status"
        :stage="taskStore.stage"
        :progress="taskStore.progress"
        :stage-timings="taskStore.stageTimings"
        :overall-risk="taskStore.overallRisk"
        :error="taskStore.error"
        :elapsed="taskStore.elapsed"
        />
        </div>
        <div class="head-actions">
        <button class="btn" type="button" @click="router.push('/')">新建比对</button>
        </div>
    </header>

    <p v-if="loadError" class="card err">{{ loadError }}</p>

    <template v-if="reportStore.report">
        <section
          v-if="reportStore.report.truncation"
          class="card truncation-warning"
        >
          <h3 class="section-title">回收件页数已截取</h3>
          <p>
            回收 PDF 共
            <strong>{{ reportStore.report.truncation.original_pdf_page_count }}</strong> 页,
            超过原始合同
            <strong>{{ reportStore.report.truncation.original_doc_page_count }}</strong> 页
            <span class="muted small">
              ({{
                reportStore.report.truncation.doc_page_count_source === 'explicit'
                  ? '外部显式传入'
                  : 'OOXML 估算'
              }})
            </span>,
            已截取前
            <strong>{{ reportStore.report.truncation.truncated_pdf_page_count }}</strong> 页
            进行比对,超出部分未纳入本次比对。
          </p>
        </section>
        <section
          v-if="reportStore.report.recognition_status === 'needs_review'"
          class="card recognition-warning"
        >
          <h3 class="section-title">部分页面识别质量不足</h3>
          <p>仅关联异常页面的差异会标记为待复核；其他可靠页面上的确认变化保持原结论。</p>
          <ul>
            <li
              v-for="item in reportStore.report.recognition_diagnostics.filter((d) => !d.reliable)"
              :key="item.page_index"
            >
              第 {{ item.page_index + 1 }} 页：{{ item.reasons.join('；') }}
            </li>
          </ul>
        </section>

        <section
          v-if="reportStore.report.location_status !== 'complete'"
          class="card recognition-warning"
        >
          <h3 class="section-title">部分页面无法完整定位高亮</h3>
          <p>这只影响 PDF 标注位置，不改变文字比对和合同变化结论。</p>
          <ul>
            <li
              v-for="item in reportStore.report.recognition_diagnostics.filter((d) => d.location_status !== 'complete')"
              :key="`location-${item.page_index}`"
            >
              第 {{ item.page_index + 1 }} 页：坐标覆盖率 {{ Math.round(item.bbox_coverage * 100) }}%
            </li>
          </ul>
        </section>

        <section class="card">
        <div class="summary">
        <div class="sum-item">
            <span :class="['verdict-badge', `verdict-${reportStore.report?.change_status ?? 'clean'}`]">
              {{ changeStatusText }}
            </span>
        </div>
        <div class="sum-item">
            <span class="sum-num">{{ reportStore.counts.total }}</span>
            <span class="muted">条款总数</span>
        </div>
        <div class="sum-item">
            <span class="sum-num warn">{{ reportStore.counts.modified }}</span>
            <span class="muted">已修改</span>
        </div>
        <div class="sum-item">
            <span class="sum-num">{{ reportStore.counts.added }}</span>
            <span class="muted">PDF 新增</span>
        </div>
        <div class="sum-item">
            <span class="sum-num">{{ reportStore.counts.deleted }}</span>
           <span class="muted">缺失</span>
       </div>
       </div>
       </section>

        <section v-if="hasKeyElements" class="card">
        <h3 class="section-title">高风险要素校验</h3>
        <KeyElementTable :elements="reportStore.keyElements" />
        </section>

        <section class="card">
        <div class="list-head">
        <h3 class="section-title" style="margin: 0">条款差异</h3>
        <div class="filters">
            <button :class="['chip', { on: filter === 'all' }]" @click="filter = 'all'">全部</button>
            <button v-if="hasRiskDiffs" :class="['chip', { on: filter === 'risk' }]" @click="filter = 'risk'">有风险</button>
            <button :class="['chip', { on: filter === 'modified' }]" @click="filter = 'modified'">已修改</button>
        </div>
        </div>
        <DiffList
          :diffs="visibleDiffs"
          :selected-clause-id="selectedClauseId"
          @select="onSelectClause"
        />
        </section>

        <section v-if="reportStore.unmatched.length" class="card">
        <h3 class="section-title">未对齐条款({{ reportStore.counts.unmatched }})</h3>
        <DiffList
          :diffs="reportStore.unmatched"
          empty-hint="无"
          :selected-clause-id="selectedClauseId"
          @select="onSelectClause"
        />
        </section>

        <div v-if="hasBidirectionalPdfPreview" class="pdf-preview-grid">
        <section class="card pdf-preview-card">
        <div class="list-head">
        <h3 class="section-title" style="margin: 0">原件渲染 PDF 预览</h3>
        <button class="chip" @click="showSourcePdf = !showSourcePdf">{{ showSourcePdf ? '收起' : '展开' }}</button>
        </div>
        <PdfViewer
        v-if="showSourcePdf"
        :pdf-url="originalPdfUrl"
        :page-meta="reportStore.report.source_page_meta"
        :diffs="reportStore.sourcePdfHighlights"
        side="source"
        fit-to-container
        contained-scroll
        :scroll-progress="sourceScrollProgress"
        :selected-clause-id="selectedClauseId"
        @select-clause="onSelectClause"
        @scroll-progress="syncSourcePdfScroll"
        />
        <p v-if="reportStore.report.source_annotation_status === 'partial'" class="muted small">
          {{ reportStore.report.source_annotation_reason }}
        </p>
        <p v-else class="muted">展开查看原件 PDF 页面与旧值/删除内容的高亮区域。</p>
        </section>

        <section class="card pdf-preview-card">
        <div class="list-head">
        <h3 class="section-title" style="margin: 0">回收件 PDF 预览</h3>
        <button class="chip" @click="showPdf = !showPdf">{{ showPdf ? '收起' : '展开' }}</button>
        </div>
        <PdfViewer
        v-if="showPdf"
        :pdf-url="pdfUrl"
        :page-meta="reportStore.report.page_meta"
        :diffs="reportStore.pdfHighlights"
        side="target"
        fit-to-container
        contained-scroll
        :scroll-progress="targetScrollProgress"
        :selected-clause-id="selectedClauseId"
        @select-clause="onSelectClause"
        @scroll-progress="syncTargetPdfScroll"
        />
        <p v-else class="muted">展开查看回收件 PDF 页面与新值/新增内容的高亮区域。</p>
        </section>
        </div>

        <template v-else>
        <section v-if="hasSourcePdfPreview" class="card pdf-preview-card">
        <div class="list-head">
        <h3 class="section-title" style="margin: 0">原件 PDF 预览</h3>
        <button class="chip" @click="showSourcePdf = !showSourcePdf">{{ showSourcePdf ? '收起' : '展开' }}</button>
        </div>
        <PdfViewer
        v-if="showSourcePdf"
        :pdf-url="originalPdfUrl"
        :page-meta="reportStore.report.source_page_meta"
        :diffs="reportStore.sourcePdfHighlights"
        side="source"
        :selected-clause-id="selectedClauseId"
        @select-clause="onSelectClause"
        />
        <p v-if="reportStore.report.source_annotation_status === 'partial'" class="muted small">
          {{ reportStore.report.source_annotation_reason }}
        </p>
        <p v-else class="muted">展开查看原件 PDF 页面与旧值/删除内容的高亮区域。</p>
        </section>

        <section v-else-if="reportStore.report.source_annotation_reason" class="card source-annotation-note">
          <p class="muted">原件侧标注不可用：{{ reportStore.report.source_annotation_reason }}</p>
        </section>

        <section v-if="reportStore.hasPdfHighlights" class="card">
        <div class="list-head">
        <h3 class="section-title" style="margin: 0">回收件 PDF 预览</h3>
        <button class="chip" @click="showPdf = !showPdf">{{ showPdf ? '收起' : '展开' }}</button>
        </div>
        <PdfViewer
        v-if="showPdf"
        :pdf-url="pdfUrl"
        :page-meta="reportStore.report.page_meta"
        :diffs="reportStore.pdfHighlights"
        side="target"
        :selected-clause-id="selectedClauseId"
        @select-clause="onSelectClause"
        />
        <p v-else class="muted">展开查看回收件 PDF 页面与高亮区域。</p>
        </section>
        </template>
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
.recognition-warning {
  border-left: 4px solid var(--risk-medium);
  background: var(--risk-medium-bg);
}
.pdf-preview-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  align-items: start;
  gap: 16px;
}
.pdf-preview-card {
  min-width: 0;
  overflow-x: auto;
}
@media (max-width: 1100px) {
  .pdf-preview-grid {
    gap: 6px;
  }
  .pdf-preview-card {
    padding: 8px;
  }
  .pdf-preview-card .section-title {
    font-size: 14px;
  }
}
.recognition-warning p,
.recognition-warning ul {
  margin-bottom: 0;
}
/* 回收件页数截取提示(等保审计留痕):主色边框,区别于风险提示 */
.truncation-warning {
  border-left: 4px solid var(--primary);
  background: rgba(43, 95, 214, 0.05);
}
.truncation-warning p {
  margin: 0;
}
.truncation-warning strong {
  color: var(--primary);
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
.summary {
display: flex;
flex-wrap: wrap;
gap: 24px;
align-items: center;
}
.sum-item {
display: flex;
flex-direction: column;
align-items: flex-start;
}
.verdict-badge {
  display: inline-block;
  padding: 4px 12px;
  border-radius: 999px;
  font-size: 13px;
  font-weight: 500;
  line-height: 1.4;
}
.verdict-changed {
  background: var(--risk-medium-bg);
  color: var(--risk-medium);
}
.verdict-needs_review {
  background: var(--risk-medium-bg);
  color: var(--risk-medium);
}
.verdict-clean {
  background: var(--risk-none-bg);
  color: var(--risk-none);
}
.sum-num {
font-size: 22px;
font-weight: 700;
line-height: 1.1;
}
.sum-num.warn {
color: var(--risk-medium);
}
.sum-num.ok {
color: var(--risk-clean);
}
.list-head {
display: flex;
justify-content: space-between;
align-items: center;
margin-bottom: 12px;
}
.filters {
display: flex;
gap: 6px;
}
.err {
color: var(--risk-high);
}
</style>
