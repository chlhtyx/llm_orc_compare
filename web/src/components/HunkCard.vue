<script setup lang="ts">
import { computed } from 'vue'
import type { TextDiffHunk } from '@/api/types'

const props = defineProps<{
  hunk: TextDiffHunk
}>()

const TAG_CN: Record<string, string> = {
  replace: '替换',
  delete: '删除',
  insert: '新增',
}

const tagCn = computed(() => TAG_CN[props.hunk.tag] ?? props.hunk.tag)
// replace 且有字符级片段时,优先渲染字符级(更精确);否则按行渲染
const useChar = computed(
  () => props.hunk.tag === 'replace' && props.hunk.char_segments.length > 0,
)
</script>

<template>
  <article class="hunk" :class="`is-${hunk.tag}`">
    <header class="hunk-head">
      <span class="hunk-tag">{{ tagCn }}</span>
    </header>
    <div class="hunk-body diff-text">
      <!-- 上下文:差异前 -->
      <div v-for="(ln, i) in hunk.context_before" :key="'cb' + i" class="diff-eq ctx">{{ ln }}</div>

      <!-- Word 侧删除行 -->
      <div v-if="!useChar" v-for="(ln, i) in hunk.word_lines" :key="'w' + i" class="diff-del">- {{ ln }}</div>
      <!-- PDF 侧新增行 -->
      <div v-if="!useChar" v-for="(ln, i) in hunk.pdf_lines" :key="'p' + i" class="diff-ins">+ {{ ln }}</div>

      <!-- 字符级渲染(replace 单行对单行) -->
      <div v-if="useChar" class="diff-char">
        <template v-for="(seg, i) in hunk.char_segments" :key="i">
          <span v-if="seg.op === 'equal'" class="diff-eq">{{ seg.text }}</span>
          <span v-else-if="seg.op === 'delete'" class="diff-del">{{ seg.text }}</span>
          <span v-else-if="seg.op === 'insert'" class="diff-ins">{{ seg.text }}</span>
        </template>
      </div>

      <!-- 上下文:差异后 -->
      <div v-for="(ln, i) in hunk.context_after" :key="'ca' + i" class="diff-eq ctx">{{ ln }}</div>
    </div>
  </article>
</template>

<style scoped>
.hunk {
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  overflow: hidden;
  background: var(--surface);
}
.hunk.is-replace {
  border-left: 3px solid var(--risk-medium);
}
.hunk.is-delete {
  border-left: 3px solid var(--risk-none);
}
.hunk.is-insert {
  border-left: 3px solid var(--risk-low);
}
.hunk-head {
  padding: 4px 12px;
  background: var(--surface-2);
  border-bottom: 1px solid var(--border);
}
.hunk-tag {
  font-size: 12px;
  font-weight: 600;
  color: var(--text-muted);
}
.hunk-body {
  padding: 8px 12px;
  font-size: 13px;
  line-height: 1.7;
  white-space: pre-wrap;
  word-break: break-word;
}
.hunk-body .diff-eq {
  color: var(--text-muted);
}
.hunk-body .ctx {
  opacity: 0.6;
}
.diff-char {
  padding: 2px 0;
}
/* .diff-del / .diff-ins 复用 main.css 全局样式(红底删除 / 绿底新增) */
</style>
