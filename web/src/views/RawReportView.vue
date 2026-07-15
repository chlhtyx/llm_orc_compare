<script setup lang="ts">
import { computed, onBeforeUnmount, ref } from 'vue'
import { useRouter } from 'vue-router'
import ProgressTracker from '@/components/ProgressTracker.vue'
import HunkCard from '@/components/HunkCard.vue'
import { ApiError } from '@/api/raw'
import { useRawTaskStore } from '@/stores/rawTask'
import type { TextDiffReport } from '@/api/types'

const props = defineProps<{ taskId: string }>()
const router = useRouter()
const rawTaskStore = useRawTaskStore()

const loadError = ref<string | null>(null)

// 进入报告页时接管任务:若 store 已是当前任务(submit 后跳转)则不重复加载。
async function attach(): Promise<void> {
  if (rawTaskStore.taskId === props.taskId && rawTaskStore.status !== 'failed') return
  try {
    await rawTaskStore.load(props.taskId)
  } catch (e) {
    loadError.value = e instanceof ApiError ? e.message : `加载失败: ${(e as Error).message}`
  }
}

void attach()

const report = computed<TextDiffReport | null>(() => rawTaskStore.report)

onBeforeUnmount(() => {
  rawTaskStore.dispose()
})

// 统计卡片
const stats = computed(() => report.value?.stats ?? {})
const similarityPct = computed(() => {
  const s = stats.value.similarity
  return typeof s === 'number' ? Math.round(s * 100) : null
})
</script>

<template>
  <div class="report">
    <header class="card report-head">
      <div class="head-left">
        <h2 class="page-title">
          无标注比对报告
          <span class="task-id muted">task {{ taskId }}</span>
        </h2>
        <ProgressTracker
          :status="rawTaskStore.status"
          :stage="rawTaskStore.stage"
          :progress="rawTaskStore.progress"
          :overall-risk="null"
          :error="rawTaskStore.error"
          :elapsed="rawTaskStore.elapsed"
        />
      </div>
      <div class="head-actions">
        <button class="btn" type="button" @click="router.push('/raw')">新建比对</button>
      </div>
    </header>

    <p v-if="loadError" class="card err">{{ loadError }}</p>

    <template v-if="report">
      <section v-if="report.recognition_status === 'needs_review'" class="card recognition-warning">
        <h3 class="section-title">识别质量不足</h3>
        <p>当前文本差异仅供人工复核：</p>
        <ul>
          <li
            v-for="item in report.recognition_diagnostics.filter((d) => !d.reliable)"
            :key="item.page_index"
          >
            第 {{ item.page_index + 1 }} 页：{{ item.reasons.join('；') }}
          </li>
        </ul>
      </section>

      <section class="card">
        <div class="summary">
          <div class="sum-item">
            <span class="sum-num">{{ similarityPct ?? '-' }}%</span>
            <span class="muted">文本相似度</span>
          </div>
          <div class="sum-item">
            <span class="sum-num">{{ stats.equal_lines ?? 0 }}</span>
            <span class="muted">一致行</span>
          </div>
          <div class="sum-item">
            <span class="sum-num warn">{{ stats.replaced ?? 0 }}</span>
            <span class="muted">替换行</span>
          </div>
          <div class="sum-item">
            <span class="sum-num warn">{{ stats.inserted ?? 0 }}</span>
            <span class="muted">新增行</span>
          </div>
          <div class="sum-item">
            <span class="sum-num warn">{{ stats.deleted ?? 0 }}</span>
            <span class="muted">删除行</span>
          </div>
        </div>
      </section>

      <section class="card">
        <h3 class="section-title">差异片段({{ report.hunks.length }})</h3>
        <div v-if="report.hunks.length" class="hunk-list">
          <HunkCard v-for="(h, i) in report.hunks" :key="i" :hunk="h" />
        </div>
        <p v-else class="muted">两份文档文本一致,无差异。</p>
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
.sum-num {
  font-size: 22px;
  font-weight: 700;
  line-height: 1.1;
}
.sum-num.warn {
  color: var(--risk-medium);
}
.hunk-list {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.err {
  color: var(--risk-high);
}
.recognition-warning {
  border-left: 4px solid var(--risk-medium);
  background: var(--risk-medium-bg);
}
.recognition-warning p,
.recognition-warning ul {
  margin-bottom: 0;
}
</style>
