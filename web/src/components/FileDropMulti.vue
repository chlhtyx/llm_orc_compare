<script setup lang="ts">
import { computed, ref } from 'vue'

const props = defineProps<{
  accept: string
  label: string
  hint?: string
  modelValue?: File[]
}>()

const emit = defineEmits<{
  'update:modelValue': [files: File[]]
}>()

const dragging = ref(false)
const input = ref<HTMLInputElement | null>(null)

const files = computed<File[]>(() => props.modelValue ?? [])

function emitFiles(next: File[]): void {
  emit('update:modelValue', next)
}

function filterByAccept(list: File[]): File[] {
  if (!props.accept) return list
  // accept 形如 ".pdf,.jpg";扩展名匹配大小写不敏感
  const exts = props.accept
    .split(',')
    .map((s) => s.trim().toLowerCase())
    .filter(Boolean)
  return list.filter((f) => {
    const lower = f.name.toLowerCase()
    return exts.some((ext) => lower.endsWith(ext))
  })
}

function pick(): void {
  input.value?.click()
}

function onDrop(e: DragEvent): void {
  dragging.value = false
  const dropped = e.dataTransfer?.files ? Array.from(e.dataTransfer.files) : []
  if (dropped.length === 0) return
  const filtered = filterByAccept(dropped)
  if (filtered.length === 0) return
  // 追加而非覆盖(允许分批添加)
  emitFiles([...files.value, ...filtered])
}

function onChange(e: Event): void {
  const picked = (e.target as HTMLInputElement).files
    ? Array.from((e.target as HTMLInputElement).files as FileList)
    : []
  if (picked.length === 0) return
  emitFiles([...files.value, ...picked])
  // 清空 input.value,允许下次再选同名文件
  if (input.value) input.value.value = ''
}

function removeAt(idx: number): void {
  emitFiles(files.value.filter((_, i) => i !== idx))
}

function clearAll(): void {
  emitFiles([])
  if (input.value) input.value.value = ''
}
</script>

<template>
  <div class="drop-wrap">
    <div
      class="drop"
      :class="{ dragging, filled: files.length > 0 }"
      role="button"
      tabindex="0"
      @click="pick"
      @keydown.enter.prevent="pick"
      @dragover.prevent="dragging = true"
      @dragleave.prevent="dragging = false"
      @drop.prevent="onDrop"
    >
      <input ref="input" type="file" :accept="accept" multiple hidden @change="onChange" />
      <div class="drop-empty">
        <div class="drop-title">{{ label }}</div>
        <div v-if="hint" class="muted">{{ hint }}</div>
        <div class="muted drop-cta">支持多选;点击或拖拽文件到此处(可分批添加)</div>
      </div>
    </div>

    <ul v-if="files.length > 0" class="file-list">
      <li v-for="(f, i) in files" :key="i" class="file-row">
        <span class="file-icon">📄</span>
        <span class="file-name" :title="f.name">{{ f.name }}</span>
        <span class="file-size muted">{{ (f.size / 1024).toFixed(0) }} KB</span>
        <button class="link-btn danger" type="button" @click="removeAt(i)">移除</button>
      </li>
    </ul>

    <div v-if="files.length > 0" class="actions">
      <span class="muted count">共 {{ files.length }} 个文件</span>
      <button class="link-btn" type="button" @click="clearAll">全部清空</button>
    </div>
  </div>
</template>

<style scoped>
.drop-wrap {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.drop {
  border: 1.5px dashed var(--border);
  border-radius: var(--radius-sm);
  padding: 18px 16px;
  cursor: pointer;
  transition: border-color 0.12s, background 0.12s;
  background: var(--surface);
}
.drop:hover,
.drop.dragging {
  border-color: var(--primary);
  background: var(--surface-2);
}
.drop.filled {
  border-style: solid;
  border-color: var(--border);
}
.drop-empty {
  text-align: center;
}
.drop-title {
  font-weight: 600;
  margin-bottom: 2px;
}
.drop-cta {
  margin-top: 6px;
  font-size: 12px;
}
.file-list {
  list-style: none;
  margin: 0;
  padding: 0;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--surface);
  overflow: hidden;
}
.file-row {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 12px;
  border-bottom: 1px solid var(--border);
}
.file-row:last-child {
  border-bottom: none;
}
.file-name {
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-weight: 500;
}
.file-size {
  font-size: 12px;
}
.actions {
  display: flex;
  align-items: center;
  gap: 12px;
  justify-content: flex-end;
}
.count {
  font-size: 12px;
}
.link-btn {
  background: none;
  border: none;
  color: var(--primary);
  cursor: pointer;
  font-size: 13px;
  padding: 0;
}
.link-btn.danger {
  color: var(--risk-high);
}
</style>
