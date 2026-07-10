<script setup lang="ts">
import { computed } from 'vue'
import type { KeyElement, KeyElementKind } from '@/api/types'

const props = defineProps<{ elements: KeyElement[] }>()

const KIND_TEXT: Record<KeyElementKind, string> = {
  amount: '金额',
  date: '日期',
  ratio: '比例',
  term: '期限',
  breach: '违约责任',
  jurisdiction: '管辖法院',
  effective: '生效条件',
  seal: '印章',
}

const rows = computed(() => props.elements)
</script>

<template>
  <div class="ke-wrap">
    <p v-if="!rows.length" class="muted">未抽取到高风险要素。</p>
    <table v-else class="tbl">
      <thead>
        <tr>
          <th style="width: 120px">要素</th>
          <th>Word 原值</th>
          <th>PDF 值</th>
          <th style="width: 90px">状态</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="(e, i) in rows" :key="i" :class="{ changed: e.changed }">
          <td>{{ KIND_TEXT[e.kind] ?? e.kind }}</td>
          <td><code>{{ e.word_value || '—' }}</code></td>
          <td>
            <code v-if="!e.changed">{{ e.pdf_value || '—' }}</code>
            <code v-else class="chg">{{ e.pdf_value || '—' }}</code>
          </td>
          <td>
            <span v-if="e.changed" class="badge badge-high">已变更</span>
            <span v-else class="muted">一致</span>
          </td>
        </tr>
      </tbody>
    </table>
  </div>
</template>

<style scoped>
.ke-wrap {
  overflow-x: auto;
}
code {
  font-family: var(--mono);
  font-size: 12.5px;
  background: var(--surface-2);
  padding: 1px 5px;
  border-radius: 4px;
}
.chg {
  background: var(--risk-high-bg);
  color: var(--risk-high);
}
tr.changed {
  background: rgba(217, 48, 37, 0.04);
}
</style>
