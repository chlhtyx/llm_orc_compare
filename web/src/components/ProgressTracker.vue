<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import OverallBadge from './OverallBadge.vue'
import type { OverallRisk, TaskStatus } from '@/api/types'

const props = defineProps<{
  status: TaskStatus
  stage: string
  progress: number
  stageTimings?: Record<string, number>
  overallRisk: OverallRisk | null
  error?: string | null
  elapsed?: number | null
}>()

const STAGE_TEXT: Record<string, string> = {
  parse_word: '解析 Word',
  word_parsing: '解析 Word',
  word_done: '解析 Word',
  ocr_pdf: 'PDF OCR',
  ocr: 'PDF OCR',
  ocr_done: 'PDF OCR',
  structure: '条款切分',
  structure_done: '条款切分',
  align: '条款对齐',
  align_done: '条款对齐',
  compare: '比对检测',
  compare_done: '比对检测',
  report: '生成报告',
  normalize: '文本规范化',
  diff: 'LLM 差异比对',
  done: '完成',
  failed: '处理失败',
}

const stageText = computed(() => STAGE_TEXT[props.stage] ?? props.stage ?? '排队中')
const pct = computed(() => Math.min(100, Math.round((props.progress ?? 0) * 100)))

const TIMING_TEXT: Record<string, string> = {
  word: 'Word 解析',
  ocr: 'PDF 读取/OCR',
  structure: '条款切分',
  align: '条款对齐',
  compare: '比对与报告',
  normalize: '文本规范化',
  diff: 'LLM 差异比对',
}

const TIMING_ORDER = ['word', 'ocr', 'structure', 'align', 'compare', 'normalize', 'diff']
const timingRows = computed(() => {
  const timings = props.stageTimings ?? {}
  return Object.entries(timings)
    .filter(([, seconds]) => Number.isFinite(seconds))
    .sort(([left], [right]) => {
      const leftIndex = TIMING_ORDER.indexOf(left)
      const rightIndex = TIMING_ORDER.indexOf(right)
      return (leftIndex < 0 ? TIMING_ORDER.length : leftIndex)
        - (rightIndex < 0 ? TIMING_ORDER.length : rightIndex)
    })
    .map(([key, seconds]) => ({ key, label: TIMING_TEXT[key] ?? key, seconds }))
})

const isTerminal = computed(() => props.status === 'done' || props.status === 'failed')

// 运行中本地计时(从组件挂载起算),终结后用后端返回的精确值。
const liveElapsed = ref(0)
const mountTime = Date.now()
let timer: ReturnType<typeof setInterval> | null = null

watch(
  () => props.status,
  (s) => {
    if (s === 'pending' || s === 'running') {
      if (!timer) timer = setInterval(() => {
        liveElapsed.value = (Date.now() - mountTime) / 1000
      }, 200)
    } else if (timer) {
      clearInterval(timer)
      timer = null
    }
  },
  { immediate: true },
)

onBeforeUnmount(() => {
  if (timer) clearInterval(timer)
})

const elapsedSec = computed(() => (isTerminal.value ? props.elapsed : liveElapsed.value) ?? 0)

function fmtElapsed(sec: number): string {
  if (sec <= 0) return '0s'
  if (sec < 60) return `${sec.toFixed(1)}s`
  const m = Math.floor(sec / 60)
  const s = Math.round(sec % 60)
  return `${m}m${s}s`
}
</script>

<template>
  <div class="tracker">
    <div class="tracker-head">
      <span class="stage">{{ stageText }}</span>
      <span class="pct muted">{{ pct }}%</span>
    </div>
    <div class="progress"><span :style="{ width: pct + '%' }" /></div>
    <div class="tracker-foot">
      <span class="elapsed">总耗时 {{ fmtElapsed(elapsedSec) }}</span>
      <template v-if="status === 'failed'">
        <span class="err">失败:{{ error || '未知错误' }}</span>
      </template>
      <template v-else-if="status === 'done'">
        <OverallBadge v-if="overallRisk" :level="overallRisk" />
        <span v-else class="muted">完成</span>
      </template>
      <template v-else>
        <span class="muted">{{ status === 'pending' ? '等待处理…' : '处理中…' }}</span>
      </template>
    </div>
    <div v-if="timingRows.length" class="stage-timings" aria-label="各阶段耗时">
      <div v-for="row in timingRows" :key="row.key" class="timing-row">
        <span>{{ row.label }}</span>
        <span>{{ fmtElapsed(row.seconds) }}</span>
      </div>
    </div>
  </div>
</template>

<style scoped>
.tracker-head {
  display: flex;
  justify-content: space-between;
  margin-bottom: 6px;
}
.stage {
  font-weight: 600;
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
.stage-timings {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(132px, 1fr));
  gap: 6px 14px;
  margin-top: 10px;
  padding-top: 10px;
  border-top: 1px solid var(--border);
}
.timing-row {
  display: flex;
  justify-content: space-between;
  gap: 10px;
  color: var(--text-muted);
  font-size: 12px;
}
.timing-row span:last-child {
  color: var(--text);
  font-variant-numeric: tabular-nums;
}
</style>
