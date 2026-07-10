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
  similarityIdentical: '',
  similarityModified: '',
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
        similarity_identical: opts.similarityIdentical ? Number(opts.similarityIdentical) : undefined,
        similarity_modified: opts.similarityModified ? Number(opts.similarityModified) : undefined,
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
        <label class="check">
          <input v-model="opts.enableLlmJudge" type="checkbox" />
          <span>启用 LLM 判定(对疑似修改做语义复核)</span>
        </label>
        <div class="grid-2 thresh">
          <div class="field">
            <label>一致阈值</label>
            <input v-model="opts.similarityIdentical" class="input" placeholder="默认 0.98" />
            <span class="hint">相似度 ≥ 该值视为一致</span>
          </div>
          <div class="field">
            <label>修改阈值</label>
            <input v-model="opts.similarityModified" class="input" placeholder="默认 0.85" />
            <span class="hint">低于该值视为实质修改</span>
          </div>
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
.thresh {
  margin-top: 4px;
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
