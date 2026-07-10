<script setup lang="ts">
import { computed, ref } from 'vue'

const props = defineProps<{
  accept: string
  label: string
  hint?: string
  modelValue?: File | null
}>()

const emit = defineEmits<{
  'update:modelValue': [file: File | null]
}>()

const dragging = ref(false)
const input = ref<HTMLInputElement | null>(null)

const file = computed(() => props.modelValue ?? null)

function pick(): void {
  input.value?.click()
}

function onDrop(e: DragEvent): void {
  dragging.value = false
  const f = e.dataTransfer?.files?.[0]
  if (f) emit('update:modelValue', f)
}

function onChange(e: Event): void {
  const f = (e.target as HTMLInputElement).files?.[0] ?? null
  emit('update:modelValue', f)
}

function clear(): void {
  emit('update:modelValue', null)
  if (input.value) input.value.value = ''
}
</script>

<template>
  <div
    class="drop"
    :class="{ dragging, filled: !!file }"
    role="button"
    tabindex="0"
    @click="pick"
    @keydown.enter.prevent="pick"
    @dragover.prevent="dragging = true"
    @dragleave.prevent="dragging = false"
    @drop.prevent="onDrop"
  >
    <input ref="input" type="file" :accept="accept" hidden @change="onChange" />
    <template v-if="file">
      <div class="file-row">
        <span class="file-icon">📄</span>
        <span class="file-name" :title="file.name">{{ file.name }}</span>
        <span class="file-size muted">{{ (file.size / 1024).toFixed(0) }} KB</span>
        <button class="link-btn" type="button" @click.stop="clear">更换</button>
      </div>
    </template>
    <template v-else>
      <div class="drop-empty">
        <div class="drop-title">{{ label }}</div>
        <div v-if="hint" class="muted">{{ hint }}</div>
        <div class="muted drop-cta">点击或拖拽文件到此处</div>
      </div>
    </template>
  </div>
</template>

<style scoped>
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
.file-row {
  display: flex;
  align-items: center;
  gap: 8px;
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
.link-btn {
  background: none;
  border: none;
  color: var(--primary);
  cursor: pointer;
  font-size: 13px;
  padding: 0;
}
</style>
