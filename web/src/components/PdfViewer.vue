<script setup lang="ts">
import { computed } from 'vue'
import type { Diff, PageMeta } from '@/api/types'

// 当前为 MVP 预览:用浏览器原生 <iframe> 渲染上传的 PDF,
// 并列出与差异条款关联的高亮区域(归一化坐标)。
// 精确的页面叠加渲染需接入 pdf.js(用 page_meta 反算 viewport),见 README“待办”。
const props = defineProps<{
  pdfUrl?: string | null
  pageMeta: PageMeta[]
  diffs: Diff[]
}>()

const regions = computed(() =>
  props.diffs
    .filter((d) => d.page_regions.length)
    .flatMap((d) =>
      d.page_regions.map((r, i) => ({
        key: `${d.alignment_id}-${i}`,
        label: [d.number, d.title].filter(Boolean).join(' · ') || d.alignment_id,
        risk: d.risk_level,
        page: r.page_index,
        box: r.bbox,
      })),
    ),
)
</script>

<template>
  <div class="pv">
    <div v-if="pdfUrl" class="pv-frame">
      <iframe :src="pdfUrl" title="PDF 预览" />
    </div>
    <p v-else class="muted pv-empty">
      未提供 PDF(报告查看模式下无法回溯上传文件,仅展示区域信息)。
    </p>

    <section v-if="regions.length" class="pv-regions">
      <h4 class="section-title">高亮区域({{ regions.length }})</h4>
      <ul class="region-list">
        <li v-for="r in regions" :key="r.key">
          <span class="badge" :class="`badge-${r.risk}`">{{ r.risk }}</span>
          <span class="r-label">{{ r.label }}</span>
          <span class="muted">第 {{ r.page + 1 }} 页 · bbox {{ r.box.map((v) => v.toFixed(2)).join(',') }}</span>
        </li>
      </ul>
    </section>
  </div>
</template>

<style scoped>
.pv {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.pv-frame {
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  overflow: hidden;
  background: #525659;
}
.pv-frame iframe {
  width: 100%;
  height: 560px;
  border: 0;
  display: block;
}
.pv-empty {
  font-style: italic;
}
.region-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 6px;
  font-size: 13px;
}
.region-list li {
  display: flex;
  align-items: center;
  gap: 8px;
}
.r-label {
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
</style>
