<script setup lang="ts">
import { computed } from 'vue'
import DiffItem from './DiffItem.vue'
import type { Diff } from '@/api/types'

const props = defineProps<{ diffs: Diff[]; emptyHint?: string }>()

const items = computed(() => props.diffs)
</script>

<template>
  <div class="diff-list">
    <p v-if="!items.length" class="muted empty">{{ emptyHint ?? '无条款差异。' }}</p>
    <DiffItem v-for="d in items" :key="d.alignment_id" :diff="d" />
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
