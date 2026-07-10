<script setup lang="ts">
import { computed } from 'vue'
import OverallBadge from './OverallBadge.vue'
import type { Diff } from '@/api/types'

const props = defineProps<{
  diff: Diff
  selected?: boolean
}>()

const emit = defineEmits<{
  select: [id: string]
}>()

const heading = computed(() => {
  const parts = [props.diff.number, props.diff.title].filter(Boolean)
  return parts.length ? parts.join(' · ') : `#${props.diff.alignment_id}`
})

const hasInlineDiff = computed(
  () => props.diff.status === 'modified' && props.diff.segments.length > 0,
)
</script>

<template>
  <article
    class="diff-item"
    :class="{
      [`is-${diff.status}`]: true,
      'diff-item--sel': selected,
    }"
    @click="emit('select', diff.alignment_id)"
  >
    <header class="diff-head">
      <span class="diff-title">{{ heading }}</span>
      <div class="diff-tags">
        <OverallBadge :level="diff.risk_level" />
        <span class="status-pill">{{ diff.status }}</span>
      </div>
    </header>

    <ul v-if="diff.risk_reasons.length" class="reasons">
      <li v-for="(r, i) in diff.risk_reasons" :key="i">⚠ {{ r }}</li>
    </ul>

    <div v-if="hasInlineDiff" class="diff-body diff-text">
      <template v-for="(seg, i) in diff.segments" :key="i">
        <span v-if="seg.op === 'equal'" class="diff-eq">{{ seg.text }}</span>
        <span v-else-if="seg.op === 'delete'" class="diff-del">{{ seg.text }}</span>
        <span v-else-if="seg.op === 'insert'" class="diff-ins">{{ seg.text }}</span>
      </template>
    </div>
    <p v-else-if="diff.status === 'identical'" class="muted same-text">内容一致</p>
    <p v-else-if="diff.status === 'added'" class="muted">仅在 PDF 中出现的新增条款</p>
    <p v-else-if="diff.status === 'deleted'" class="muted">仅在 Word 中存在、PDF 缺失</p>

    <div v-if="diff.page_regions.length" class="regions muted">
      关联 PDF 区域:{{ diff.page_regions.length }} 处
      <span v-if="diff.page_regions[0]"> · 第 {{ diff.page_regions[0].page_index + 1 }} 页起</span>
    </div>
  </article>
</template>

<style scoped>
.diff-item {
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  padding: 12px 14px;
  background: var(--surface);
  cursor: pointer;
  transition: border-color 0.12s, background 0.12s;
}
.diff-item:hover {
  background: var(--surface-2);
}
.diff-item--sel {
  border-color: var(--primary);
  background: rgba(59,130,246,.06);
}
.diff-item.is-modified {
  border-left: 3px solid var(--risk-medium);
}
.diff-item.is-added {
  border-left: 3px solid var(--risk-low);
}
.diff-item.is-deleted {
  border-left: 3px solid var(--risk-none);
}
.diff-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  margin-bottom: 8px;
}
.diff-title {
  font-weight: 600;
  word-break: break-word;
}
.diff-tags {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-shrink: 0;
}
.status-pill {
  font-size: 11px;
  color: var(--text-muted);
  border: 1px solid var(--border);
  border-radius: 4px;
  padding: 1px 6px;
  text-transform: uppercase;
}
.reasons {
  margin: 0 0 8px;
  padding-left: 16px;
  color: var(--risk-medium);
  font-size: 13px;
}
.diff-body {
  margin-top: 6px;
  padding: 10px;
  background: var(--surface-2);
  border-radius: var(--radius-sm);
  max-height: 240px;
  overflow: auto;
}
.same-text {
  font-style: italic;
}
.regions {
  margin-top: 8px;
  font-size: 12px;
}
</style>
