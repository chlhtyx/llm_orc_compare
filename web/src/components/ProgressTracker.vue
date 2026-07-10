<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import OverallBadge from './OverallBadge.vue'
import type { OverallRisk, TaskStatus } from '@/api/types'

const props = defineProps<{
  status: TaskStatus
  stage: string
  progress: number
  overallRisk: OverallRisk | null
  error?: string | null
  elapsed?: number | null
}>()

const STAGE_TEXT: Record<string, string> = {
  parse_word: '解析 Word',
  ocr_pdf: 'PDF OCR',
  structure: '条款切分',
  align: '条款对齐',
  compare: '比对检测',
  report: '生成报告',
}

const stageText = computed(() => STAGE_TEXT[props.stage] ?? props.stage ?? '排队中')
const pct = computed(() => Math.min(100, Math.round((props.progress ?? 0) * 100)))

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
      <span class="elapsed">{{ fmtElapsed(elapsedSec) }}</span>
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
</style>
