<script setup lang="ts">
import { computed, ref } from 'vue'
import { useRouter } from 'vue-router'
import FileDrop from '@/components/FileDrop.vue'
import { ApiError } from '@/api/raw'
import { useRawTaskStore } from '@/stores/rawTask'

const router = useRouter()
const rawTaskStore = useRawTaskStore()

const sourceFile = ref<File | null>(null)
const targetFile = ref<File | null>(null)
const charLevel = ref(false)
const ocrBackend = ref<'llm' | 'paddleocr'>('paddleocr')
const submitting = ref(false)
const submitError = ref<string | null>(null)

const sourceValid = computed(() => {
  const name = sourceFile.value?.name.toLowerCase() ?? ''
  return name.endsWith('.docx') || name.endsWith('.pdf')
})
const targetValid = computed(() => !!targetFile.value?.name.toLowerCase().endsWith('.pdf'))
const canSubmit = computed(
  () => sourceValid.value && targetValid.value && !submitting.value,
)

async function onSubmit(): Promise<void> {
  submitError.value = null
  if (!sourceFile.value || !targetFile.value) return
  submitting.value = true
  try {
    const id = await rawTaskStore.submit({
      source: sourceFile.value,
      target: targetFile.value,
      options: { char_level: charLevel.value, ocr_backend: ocrBackend.value },
    })
    router.push(`/raw/report/${id}`)
  } catch (e) {
    submitError.value = e instanceof ApiError ? e.message : `提交失败: ${(e as Error).message}`
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <div class="submit">
    <section class="card">
      <h2 class="page-title">无标注比对</h2>
      <p class="muted page-desc">
        将 Word 与 PDF 都转成纯文本后,用 difflib 行级比对直接列出差异片段。
        不经过条款对齐、风险分级,纯文本差异(类似 git diff)。
      </p>

      <div class="grid-2">
        <div class="field">
          <label>原始合同(Word / PDF)</label>
          <FileDrop
            v-model="sourceFile"
            accept=".docx,.pdf"
            label="选择 .docx 或 .pdf 文件"
            hint="提取为纯文本(保留段落结构)"
          />
          <span v-if="sourceFile && !sourceValid" class="err">文件必须是 .docx 或 .pdf 格式</span>
        </div>
        <div class="field">
          <label>回收件(PDF 扫描件)</label>
          <FileDrop
            v-model="targetFile"
            accept=".pdf"
            label="选择 .pdf 文件"
            hint="OCR 识别为纯文本"
          />
          <span v-if="targetFile && !targetValid" class="err">文件必须是 .pdf 格式</span>
        </div>
      </div>
    </section>

    <section class="card">
      <h3 class="section-title">比对选项(可选)</h3>
      <div class="opts">
        <div class="field">
          <label>识别引擎</label>
          <div class="radio-row">
             <label class="radio">
              <input type="radio" value="paddleocr" v-model="ocrBackend" />
              <span>paddleocr(专用 OCR 模型)</span>
            </label>
            <label class="radio">
              <input type="radio" value="llm" v-model="ocrBackend" />
              <span>llm(通用 VL 模型)</span>
            </label>
           
          </div>
          <span class="hint">识别 PDF 扫描件版面所用的引擎。两套引擎在设置页分别配置,需确保所选引擎已配置。</span>
        </div>
        <label class="check">
          <input v-model="charLevel" type="checkbox" />
          <span>字符级细化(对替换行做行内红/绿标记)</span>
        </label>
      </div>
    </section>

    <section class="card">
      <div class="actions">
        <button class="btn btn-primary" :disabled="!canSubmit" @click="onSubmit">
          {{ submitting ? '提交中…' : '开始比对' }}
        </button>
      </div>
      <p v-if="submitError" class="err">{{ submitError }}</p>
    </section>
  </div>
</template>

<style scoped>
.page-title {
  margin: 0 0 4px;
  font-size: 18px;
}
.page-desc {
  margin: 0 0 16px;
}
.opts {
  display: flex;
  flex-direction: column;
  gap: 14px;
}
.check {
  display: flex;
  align-items: center;
  gap: 8px;
  cursor: pointer;
}
.radio-row {
  display: flex;
  gap: 20px;
  flex-wrap: wrap;
}
.radio {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  cursor: pointer;
  font-size: 14px;
}
.radio input {
  cursor: pointer;
}
.actions {
  display: flex;
  gap: 10px;
  align-items: center;
}
.err {
  color: var(--risk-high);
  margin-top: 8px;
  font-size: 13px;
}
</style>
