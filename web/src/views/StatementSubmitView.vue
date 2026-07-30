<script setup lang="ts">
import { computed, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import FileDropMulti from '@/components/FileDropMulti.vue'
import { ApiError } from '@/api/statement'
import { useStatementTaskStore } from '@/stores/statementTask'

const router = useRouter()
const statementStore = useStatementTaskStore()

const targetFiles = ref<File[]>([])
const submitting = ref(false)
const submitError = ref<string | null>(null)

const opts = reactive({
  ocrBackend: 'paddleocr' as 'llm' | 'paddleocr',
  enableLlmColumnDetection: true,
  customKeywords: '', // 逗号分隔的金额列关键词(可选)
})
const callbackUrl = ref('')

const allPdf = computed(() =>
  targetFiles.value.length > 0 &&
  targetFiles.value.every((f) => f.name.toLowerCase().endsWith('.pdf')),
)
const canSubmit = computed(() => allPdf.value && !submitting.value)

async function onSubmit(): Promise<void> {
  submitError.value = null
  if (targetFiles.value.length === 0) return
  submitting.value = true
  try {
    const keywords = opts.customKeywords
      .split(/[,，]/)
      .map((s) => s.trim())
      .filter(Boolean)
    const id = await statementStore.submit({
      targets: targetFiles.value,
      options: {
        ocr_backend: opts.ocrBackend,
        enable_llm_column_detection: opts.enableLlmColumnDetection,
        ...(keywords.length > 0 ? { amount_column_keywords: keywords } : {}),
      },
      callbackUrl: callbackUrl.value.trim() || undefined,
    })
    router.push(`/statement/report/${id}`)
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
      <h2 class="page-title">金额统计</h2>
      <p class="muted page-desc">
        一次可上传多个对帐单 PDF(扫描件),系统 OCR 识别表格后用<b>确定性代码</b>抽取并累加金额,
        汇总所有文件的总金额。若表格含「合计/小计」行,会自动核对声明值与实算是否一致;
        列定位失败时由多模态 LLM 仅指认金额列(不做算术)。
      </p>

      <div class="field">
        <label>对帐单 PDF(可多选)</label>
        <FileDropMulti
          v-model="targetFiles"
          accept=".pdf"
          label="选择一个或多个 .pdf 文件"
          hint="对帐单扫描件,可分批添加"
        />
        <span v-if="targetFiles.length > 0 && !allPdf" class="err">所有文件必须是 .pdf 格式</span>
      </div>
    </section>

    <section class="card">
      <h3 class="section-title">统计选项(可选)</h3>
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
          <input v-model="opts.enableLlmColumnDetection" type="checkbox" />
          <span>启用 LLM 列定位兜底(启发式无法识别金额列时,让 LLM 仅指认哪一列是金额,不做求和)</span>
        </label>

        <div class="field">
          <label>自定义金额列关键词(可选)</label>
          <input
            v-model="opts.customKeywords"
            class="input"
            placeholder="如:金额,已收,未付(逗号分隔;留空走默认启发式)"
          />
          <span class="hint">默认启发式已覆盖 金额/已付/未付/合计 等;仅当对帐单表头非常规时才填</span>
        </div>

        <div class="field">
          <label>回调地址(可选)</label>
          <input v-model="callbackUrl" class="input" placeholder="https://your/cb · 完成后回调" />
        </div>
      </div>
    </section>

    <section class="card">
      <div class="actions">
        <button class="btn btn-primary" :disabled="!canSubmit" @click="onSubmit">
          {{ submitting ? '提交中…' : '开始统计' }}
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
