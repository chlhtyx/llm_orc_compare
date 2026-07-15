<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { ApiError, getDocxPreview } from '@/api/compare'
import type { PreviewParagraph } from '@/api/compare'

const props = defineProps<{ taskId: string }>()

const emit = defineEmits<{
  select: [alignmentHint: string]
}>()

const loading = ref(true)
const loadError = ref<string | null>(null)
const paragraphs = ref<PreviewParagraph[]>([])
// 点击高亮段时滚动到的当前段索引
const selectedIndex = ref<number | null>(null)

async function load(): Promise<void> {
  loading.value = true
  loadError.value = null
  try {
    paragraphs.value = await getDocxPreview(props.taskId)
  } catch (e) {
    loadError.value = e instanceof ApiError ? e.message : `加载失败: ${(e as Error).message}`
  } finally {
    loading.value = false
  }
}

onMounted(load)
watch(() => props.taskId, load)

const highlighted = computed(() => paragraphs.value.filter((p) => p.highlight))

function highlightClass(p: PreviewParagraph): string {
  if (p.highlight === 'deleted') return 'dp-par--deleted'
  if (p.highlight === 'modified') return `dp-par--modified dp-par--${p.risk_level}`
  return ''
}

function onClickParagraph(p: PreviewParagraph, idx: number): void {
  if (!p.highlight) return
  selectedIndex.value = idx
  // 用编号作为提示，父级可据此联动条款差异卡片
  emit('select', p.number || p.text.slice(0, 20))
}
</script>

<template>
  <div class="dp">
    <div v-if="loading" class="dp-loading muted">加载高亮预览…</div>
    <p v-else-if="loadError" class="dp-err">{{ loadError }}</p>

    <template v-else>
      <!-- 顶部图例 -->
      <div class="dp-legend">
        <span class="dp-legend-item">
          <span class="dp-swatch dp-swatch--modified" /> 已修改条款
        </span>
        <span class="dp-legend-item">
          <span class="dp-swatch dp-swatch--deleted" /> PDF 缺失条款
        </span>
        <span class="dp-legend-count muted">
          共 {{ paragraphs.length }} 段，{{ highlighted.length }} 处差异
        </span>
      </div>

      <!-- 合同正文 -->
      <div class="dp-doc">
        <p
          v-for="(p, idx) in paragraphs"
          :key="idx"
          class="dp-par"
          :class="[
            highlightClass(p),
            { 'dp-par--sel': selectedIndex === idx, 'dp-par--clickable': p.highlight },
          ]"
          @click="onClickParagraph(p, idx)"
        >
          <span v-if="p.highlight" class="dp-mark" :class="`dp-mark--${p.highlight}`">
            {{ p.highlight === 'deleted' ? '缺' : '改' }}
          </span>
          <span class="dp-text">{{ p.text }}</span>
        </p>
      </div>
    </template>
  </div>
</template>

<style scoped>
.dp {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.dp-loading,
.dp-err {
  padding: 24px;
  text-align: center;
}
.dp-err {
  color: var(--risk-high);
}
.dp-legend {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 16px;
  font-size: 12px;
}
.dp-legend-item {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}
.dp-legend-count {
  margin-left: auto;
}
.dp-swatch {
  display: inline-block;
  width: 14px;
  height: 14px;
  border-radius: 3px;
  border: 1px solid var(--border);
}
.dp-swatch--modified {
  background: #ffeb3b;
  border-color: #fdd835;
}
.dp-swatch--deleted {
  background: #ffcdd2;
  border-color: #ef9a9a;
}

.dp-doc {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  padding: 24px 28px;
  max-height: 70vh;
  overflow: auto;
  line-height: 1.8;
  font-size: 14px;
}
.dp-par {
  margin: 0 0 8px;
  padding: 2px 4px;
  border-radius: 3px;
  transition: background 0.1s;
  position: relative;
}
.dp-par--modified {
  background: #fff9c4;
  border-left: 3px solid #fdd835;
  padding-left: 6px;
}
/* 按风险等级微调边框：高风险更醒目 */
.dp-par--modified.dp-par--high {
  background: #ffeb3b;
  border-left-color: #fbc02d;
}
.dp-par--modified.dp-par--medium {
  background: #ffe082;
}
.dp-par--modified.dp-par--low {
  background: #fff59d;
}
.dp-par--deleted {
  background: #ffcdd2;
  border-left: 3px solid #ef9a9a;
  padding-left: 6px;
}
.dp-par--clickable {
  cursor: pointer;
}
.dp-par--clickable:hover {
  filter: brightness(0.97);
}
.dp-par--sel {
  box-shadow: 0 0 0 2px var(--primary);
}
.dp-mark {
  display: inline-block;
  width: 18px;
  height: 18px;
  line-height: 18px;
  text-align: center;
  border-radius: 3px;
  font-size: 11px;
  font-weight: 700;
  color: #fff;
  margin-right: 6px;
  vertical-align: 1px;
}
.dp-mark--modified {
  background: #f57f17;
}
.dp-mark--deleted {
  background: #c62828;
}
.dp-text {
  /* 让段落文字与 mark 对齐自然换行 */
}
</style>
