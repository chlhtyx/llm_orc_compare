<script setup lang="ts">
import { onMounted, reactive, ref, watch } from 'vue'
import { useModelConfigStore } from '@/stores/modelConfig'
import type { LlmConfig } from '@/api/config'

const store = useModelConfigStore()

const form = reactive({
  ocr_backend: 'mock' as 'mock' | 'vllm',
  llm_api_base: '',
  llm_api_key: '',
  llm_model: '',
  llm_timeout: 120,
  llm_max_concurrency: 4,
})

const keyDirty = ref(false)
const saved = ref(false)
const saveError = ref<string | null>(null)

// 后端可能返回 paddle/llm(旧别名),前端统一归一化为 vllm
function normalizeBackend(b: string): 'mock' | 'vllm' {
  return b === 'mock' ? 'mock' : 'vllm'
}

function syncFromConfig(c: LlmConfig | null): void {
  if (!c) return
  form.ocr_backend = normalizeBackend(c.ocr_backend)
  form.llm_api_base = c.llm_api_base || ''
  form.llm_api_key = c.llm_api_key || ''
  form.llm_model = c.llm_model || ''
  form.llm_timeout = c.llm_timeout ?? 120
  form.llm_max_concurrency = c.llm_max_concurrency ?? 4
  keyDirty.value = false
}

onMounted(async () => {
  await store.fetch()
  syncFromConfig(store.config)
  if (store.error) saveError.value = store.error
})

watch(() => store.config, (c) => syncFromConfig(c))

function onKeyInput(): void {
  keyDirty.value = true
}

async function onSave(): Promise<void> {
  saveError.value = null
  saved.value = false
  const payload: Record<string, unknown> = {
    ocr_backend: form.ocr_backend,
    llm_api_base: form.llm_api_base.trim(),
    llm_model: form.llm_model.trim(),
    llm_timeout: Number(form.llm_timeout),
    llm_max_concurrency: Number(form.llm_max_concurrency),
  }
  payload.llm_api_key = keyDirty.value ? form.llm_api_key : '********'

  const ok = await store.save(payload)
  if (ok) {
    saved.value = true
    setTimeout(() => (saved.value = false), 2500)
  } else {
    saveError.value = store.error
  }
}

async function onReset(): Promise<void> {
  await store.fetch()
  syncFromConfig(store.config)
  saveError.value = null
  saved.value = false
}

const backendOptions = [
  { value: 'mock' as const, label: 'Mock', desc: 'PDF 文本层(开发/文本 PDF)' },
  { value: 'vllm' as const, label: 'vLLM 推理', desc: '远端多模态模型(PaddleOCR-VL / 千问 VL 等)' },
]

const modelPresets = [
  { base: 'http://localhost:8000/v1', model: 'PaddleOCR-VL-1.5', label: 'PaddleOCR-VL(本地 vLLM)' },
  { base: 'https://dashscope.aliyuncs.com/compatible-mode/v1', model: 'qwen-vl-max', label: '通义千问 VL Max' },
  { base: 'https://dashscope.aliyuncs.com/compatible-mode/v1', model: 'qwen-vl-plus', label: '通义千问 VL Plus' },
  { base: 'https://open.bigmodel.cn/api/paas/v4', model: 'glm-4v', label: '智谱 GLM-4V' },
  { base: 'https://api.openai.com/v1', model: 'gpt-4o', label: 'OpenAI GPT-4o' },
]

function applyPreset(p: (typeof modelPresets)[number]): void {
  form.llm_api_base = p.base
  form.llm_model = p.model
}
</script>

<template>
  <div class="model-config">
    <div v-if="store.loading" class="muted">加载配置中…</div>

    <section class="card">
      <h2 class="page-title">OCR 引擎</h2>
      <p class="muted page-desc">
        选择 PDF 扫描件的识别方式。PaddleOCR-VL 与通用多模态 LLM 均通过 vLLM 部署,
        共用下方同一套 API 配置,按模型名路由到对应服务。
      </p>

      <div class="seg">
        <button
          v-for="opt in backendOptions"
          :key="opt.value"
          class="seg-btn"
          :class="{ active: form.ocr_backend === opt.value }"
          @click="form.ocr_backend = opt.value"
        >
          <span class="seg-label">{{ opt.label }}</span>
          <span class="seg-desc">{{ opt.desc }}</span>
        </button>
      </div>
    </section>

    <section v-if="form.ocr_backend === 'vllm'" class="card">
      <h3 class="section-title">vLLM 推理服务配置</h3>

      <div class="presets">
        <span class="muted preset-hint">快捷预设:</span>
        <button
          v-for="p in modelPresets"
          :key="p.label"
          class="chip"
          :class="{ active: form.llm_model === p.model && form.llm_api_base === p.base }"
          @click="applyPreset(p)"
        >
          {{ p.label }}
        </button>
      </div>

      <div class="form-grid">
        <div class="field">
          <label>API Base</label>
          <input v-model="form.llm_api_base" class="input" placeholder="http://localhost:8000/v1" />
          <span class="hint">vLLM / SGLang / 云端 API 的 OpenAI 兼容根地址</span>
        </div>

        <div class="field">
          <label>模型名称</label>
          <input v-model="form.llm_model" class="input" placeholder="PaddleOCR-VL-1.5" />
          <span class="hint">vLLM 加载的模型名(决定走哪个推理服务)</span>
        </div>

        <div class="field span-2">
          <label>API Key</label>
          <input
            v-model="form.llm_api_key"
            class="input"
            type="password"
            :placeholder="store.config?.llm_api_key_set ? '已设置(输入新值覆盖)' : '本地 vLLM 通常留空'"
            @input="onKeyInput"
          />
          <span class="hint">
            <template v-if="store.config?.llm_api_key_set">
              当前: {{ store.config.llm_api_key || '****' }} · 留空不修改
            </template>
            <template v-else>本地 vLLM 一般无需 Key;云端服务需要</template>
          </span>
        </div>

        <div class="field">
          <label>请求超时(秒)</label>
          <input v-model.number="form.llm_timeout" class="input" type="number" min="10" max="600" />
          <span class="hint">单页识别的超时上限</span>
        </div>

        <div class="field">
          <label>最大并发</label>
          <input v-model.number="form.llm_max_concurrency" class="input" type="number" min="1" max="32" />
          <span class="hint">逐页并行数,受推理服务限流</span>
        </div>
      </div>
    </section>

    <section v-else class="card">
      <p class="muted">
        Mock 引擎直接读取 PDF 文本层,适合开发与文本型 PDF。扫描件(无文本层)请切换到 vLLM 推理。
      </p>
    </section>

    <section class="card actions-card">
      <div class="actions">
        <button class="btn btn-primary" :disabled="store.saving" @click="onSave">
          {{ store.saving ? '保存中…' : '保存配置' }}
        </button>
        <button class="btn" type="button" @click="onReset" :disabled="store.loading">重置</button>
        <span v-if="saved" class="ok">已保存</span>
      </div>
      <p v-if="saveError" class="err">{{ saveError }}</p>
      <p v-if="store.config?.config_file" class="muted file-hint">
        配置持久化于: <code>{{ store.config.config_file }}</code>
      </p>
    </section>
  </div>
</template>

<style scoped>
.model-config {
  max-width: 760px;
}
.page-title {
  margin: 0 0 4px;
  font-size: 18px;
}
.page-desc {
  margin: 0 0 16px;
}

.seg {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: 10px;
}
.seg-btn {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 4px;
  padding: 12px 14px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--surface);
  cursor: pointer;
  text-align: left;
  transition: border-color 0.12s, background 0.12s;
}
.seg-btn:hover {
  background: var(--surface-2);
}
.seg-btn.active {
  border-color: var(--primary);
  background: rgba(43, 95, 214, 0.06);
}
.seg-label {
  font-weight: 600;
  font-size: 14px;
}
.seg-desc {
  font-size: 12px;
  color: var(--text-muted);
}

.presets {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  margin-bottom: 16px;
}
.preset-hint {
  font-size: 12px;
}
.chip {
  padding: 4px 10px;
  border: 1px solid var(--border);
  border-radius: 999px;
  background: var(--surface);
  font-size: 12px;
  cursor: pointer;
  transition: border-color 0.12s, color 0.12s, background 0.12s;
}
.chip:hover {
  border-color: var(--primary);
  color: var(--primary);
}
.chip.active {
  border-color: var(--primary);
  color: var(--primary);
  background: rgba(43, 95, 214, 0.06);
}

.form-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 14px;
}
.span-2 {
  grid-column: span 2;
}
@media (max-width: 640px) {
  .seg {
    grid-template-columns: 1fr;
  }
  .form-grid {
    grid-template-columns: 1fr;
  }
  .span-2 {
    grid-column: span 1;
  }
}

.actions {
  display: flex;
  align-items: center;
  gap: 10px;
}
.ok {
  color: var(--risk-clean);
  font-size: 13px;
  font-weight: 500;
}
.err {
  color: var(--risk-high);
  margin-top: 8px;
  font-size: 13px;
}
.file-hint {
  margin-top: 10px;
  font-size: 12px;
}
code {
  font-family: var(--mono);
  font-size: 12px;
  background: var(--surface-2);
  padding: 1px 5px;
  border-radius: 3px;
}
</style>
