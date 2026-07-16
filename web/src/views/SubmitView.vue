<script setup lang="ts">
import { computed, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import FileDrop from '@/components/FileDrop.vue'
import { ApiError } from '@/api/compare'
import { useTaskStore } from '@/stores/task'

const router = useRouter()
const taskStore = useTaskStore()

const sourceFile = ref<File | null>(null)
const targetFile = ref<File | null>(null)

const opts = reactive({
  enableLlmJudge: false,
  ocrBackend: 'paddleocr' as 'llm' | 'paddleocr',
})
const callbackUrl = ref('')

const submitting = ref(false)
const submitError = ref<string | null>(null)

const sourceValid = computed(() => !!sourceFile.value?.name.toLowerCase().endsWith('.docx'))
const targetValid = computed(() => !!targetFile.value?.name.toLowerCase().endsWith('.pdf'))
const canSubmit = computed(
  () => sourceValid.value && targetValid.value && !submitting.value,
)

async function onSubmit(): Promise<void> {
  submitError.value = null
  if (!sourceFile.value || !targetFile.value) return
  submitting.value = true
  try {
    const id = await taskStore.submit({
      source: sourceFile.value,
      target: targetFile.value,
      options: {
        enable_llm_judge: opts.enableLlmJudge,
        ocr_backend: opts.ocrBackend,
      },
      callbackUrl: callbackUrl.value.trim() || undefined,
    })
    router.push(`/report/${id}`)
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
      <h2 class="page-title">提交比对</h2>
      <p class="muted page-desc">
        上传原始 Word 合同与回收盖章的 PDF 扫描件,系统将自动识别条款篡改。
      </p>

      <div class="grid-2">
        <div class="field">
          <label>原始合同(Word)</label>
          <FileDrop
            v-model="sourceFile"
            accept=".docx"
            label="选择 .docx 文件"
            hint="以原始 Word 版本为比对基准"
          />
          <span v-if="sourceFile && !sourceValid" class="err">文件必须是 .docx 格式</span>
        </div>
        <div class="field">
          <label>回收件(PDF 扫描件)</label>
          <FileDrop
            v-model="targetFile"
            accept=".pdf"
            label="选择 .pdf 文件"
            hint="待核验的盖章扫描件"
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
              <input type="radio" value="paddleocr" v-model="opts.ocrBackend" />
              <span>paddleocr(专用 OCR 模型)</span>
            </label>
            <label class="radio">
              <input type="radio" value="llm" v-model="opts.ocrBackend" />
              <span>llm(通用 VL 模型)</span>
            </label>
          </div>
          <span class="hint">识别 PDF 扫描件版面所用的引擎。两套引擎在设置页分别配置,需确保所选引擎已配置。</span>
        </div>
        <label class="check">
          <input v-model="opts.enableLlmJudge" type="checkbox" />
          <span>启用 LLM 辅助说明(不会撤销已确认变化)</span>
        </label>
        <p class="hint zero-tolerance">
          零容忍模式：语义相似度仅用于条款对齐，任何确认的内容变化都会报告；识别证据不足时进入人工复核。
        </p>
        <div class="field">
          <label>回调地址(可选)</label>
          <input v-model="callbackUrl" class="input" placeholder="https://your/cb · 完成后回调" />
        </div>
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
.zero-tolerance {
  margin: 0;
  padding: 10px 12px;
  border-left: 3px solid var(--primary);
  background: var(--surface-2);
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
