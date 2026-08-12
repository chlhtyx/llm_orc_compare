<script setup lang="ts">
import type { Diff } from '@/api/types'

const props = defineProps<{
  diffs: Diff[]
  emptyHint?: string
  selectedClauseId?: string | null
}>()

const emit = defineEmits<{
  select: [id: string]
}>()

const statusNames: Record<Diff['status'], string> = {
  modified: '修改',
  added: '新增',
  deleted: '删除',
  identical: '一致',
}

function label(diff: Diff): string {
  return [diff.number, diff.title].filter(Boolean).join(' ') || diff.alignment_id
}

/** 与导出 HTML 报告保持相同口径：equal + delete 为原始，equal + insert 为回收。 */
function segmentsFor(diff: Diff, side: 'source' | 'target') {
  const visibleOps = side === 'source' ? ['equal', 'delete'] : ['equal', 'insert']
  return diff.segments.filter((segment) => visibleOps.includes(segment.op))
}

function select(diff: Diff): void {
  emit('select', diff.alignment_id)
}
</script>

<template>
  <div class="comparison-table-wrap">
    <table class="comparison-table">
      <thead>
        <tr>
          <th class="col-index">#</th>
          <th class="col-status">状态</th>
          <th>条款</th>
          <th>采购部合同</th>
          <th>供应商合同</th>
        </tr>
      </thead>
      <tbody>
        <template v-if="!props.diffs.length">
          <tr>
            <td colspan="5" class="empty">{{ props.emptyHint ?? '未发现内容变化' }}</td>
          </tr>
        </template>
        <template v-else>
          <tr
            v-for="(diff, index) in props.diffs"
            :key="diff.alignment_id"
            :class="{ selected: props.selectedClauseId === diff.alignment_id }"
            tabindex="0"
            @click="select(diff)"
            @keydown.enter.prevent="select(diff)"
            @keydown.space.prevent="select(diff)"
          >
            <td class="index">{{ index + 1 }}</td>
            <td>
              <span :class="['status-badge', `status-${diff.status}`]">
                {{ statusNames[diff.status] }}
              </span>
            </td>
            <td class="clause">
              <strong>{{ label(diff) }}</strong>
              <p v-if="diff.alignment_reason" class="alignment-reason">
                未对齐原因：{{ diff.alignment_reason }}
              </p>
            </td>
            <td class="diff-content">
              <template v-for="segment in segmentsFor(diff, 'source')" :key="`${diff.alignment_id}-source-${segment.op}-${segment.text}`">
                <span :class="{ del: segment.op === 'delete' }">{{ segment.text }}</span>
              </template>
              <span v-if="!segmentsFor(diff, 'source').length" class="empty">（无）</span>
            </td>
            <td class="diff-content">
              <template v-for="segment in segmentsFor(diff, 'target')" :key="`${diff.alignment_id}-target-${segment.op}-${segment.text}`">
                <span :class="{ ins: segment.op === 'insert' }">{{ segment.text }}</span>
              </template>
              <span v-if="!segmentsFor(diff, 'target').length" class="empty">（无）</span>
            </td>
          </tr>
        </template>
      </tbody>
    </table>
  </div>
</template>

<style scoped>
.comparison-table-wrap {
  overflow-x: auto;
}
.comparison-table {
  width: 100%;
  min-width: 760px;
  border-collapse: collapse;
  background: #fff;
  font-size: 13px;
}
.comparison-table th,
.comparison-table td {
  padding: 8px 10px;
  border: 1px solid #e3e3e5;
  vertical-align: top;
  text-align: left;
}
.comparison-table th {
  background: #f1f3f5;
  color: #222;
  font-size: 13px;
  font-weight: 600;
  letter-spacing: normal;
  text-transform: none;
  white-space: nowrap;
}
.comparison-table tbody tr {
  cursor: pointer;
  transition: background 0.12s;
}
.comparison-table tbody tr:hover,
.comparison-table tbody tr:focus-visible,
.comparison-table tbody tr.selected {
  background: rgba(44, 123, 229, 0.08);
  outline: none;
}
.col-index,
.index {
  width: 36px;
  color: #888;
  text-align: center !important;
}
.col-status {
  width: 64px;
}
.status-badge {
  display: inline-block;
  padding: 2px 10px;
  border-radius: 10px;
  color: #fff;
  font-size: 12px;
  line-height: 1.4;
  white-space: nowrap;
}
.status-modified { background: #e8590c; }
.status-added { background: #1971c2; }
.status-deleted { background: #e03131; }
.status-identical { background: #868e96; }
.clause {
  min-width: 130px;
  word-break: break-word;
}
.alignment-reason {
  margin: 6px 0 0;
  color: #6b7280;
  font-size: 12px;
  line-height: 1.5;
}
.diff-content {
  min-width: 220px;
  white-space: pre-wrap;
  word-break: break-word;
}
.del {
  padding: 0 1px;
  border-radius: 2px;
  background: #ffe3e3;
  text-decoration: line-through;
}
.ins {
  padding: 0 1px;
  border-radius: 2px;
  background: #d3f9d3;
}
.empty {
  color: #adb5bd;
  text-align: center;
}
</style>
