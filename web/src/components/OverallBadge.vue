<script setup lang="ts">
import { computed } from 'vue'
import type { OverallRisk, RiskLevel } from '@/api/types'

const props = defineProps<{
  level: OverallRisk | RiskLevel
  label?: string
}>()

const TEXT: Record<string, string> = {
  high: '高风险',
  medium: '中风险',
  low: '低风险',
  changed: '有变化',
  none: '无风险',
  clean: '未见篡改',
  needs_review: '待人工复核',
}

const cls = computed(() => `badge badge-${props.level}`)
const text = computed(() => props.label ?? TEXT[props.level] ?? props.level)
</script>

<template>
  <span :class="cls">{{ text }}</span>
</template>
