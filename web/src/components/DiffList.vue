<script setup lang="ts">
import DiffItem from './DiffItem.vue'
import type { Diff } from '@/api/types'

defineProps<{
  diffs: Diff[]
  emptyHint?: string
  selectedClauseId?: string | null
}>()

const emit = defineEmits<{
  select: [id: string]
}>()
</script>

<template>
  <div class="diff-list">
    <p v-if="!diffs.length" class="muted empty">{{ emptyHint ?? '无条款差异。' }}</p>
    <DiffItem
      v-for="d in diffs"
      :key="d.alignment_id"
      :diff="d"
      :selected="selectedClauseId === d.alignment_id"
      @select="emit('select', $event)"
    />
  </div>
</template>

<style scoped>
.diff-list {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.empty {
  font-style: italic;
}
</style>
