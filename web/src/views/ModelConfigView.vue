<script setup lang="ts">
import { onMounted, reactive, ref, watch } from 'vue'
import { useModelConfigStore } from '@/stores/modelConfig'
import type { LlmConfig } from '@/api/config'

const store = useModelConfigStore()

const form = reactive({
  llm_api_base: '',
  llm_api_key: '',
  llm_model: '',
  llm_timeout: 120,
  llm_max_concurrency: 4,
  // —— 语义向量引擎 ——
  embed_backend: 'mock',
  embed_api_base: '',
  embed_api_key: '',
  embed_model: '',
  embed_timeout: 60,
  pdf_render_dpi: 200,
})

const keyDirty = ref(false)
const embedKeyDirty = ref(false)
const saved = ref(false)
const saveError = ref<string | null>(null)

function syncFromConfig(c: LlmConfig | null): void {
  if (!c) return
  form.llm_api_base = c.llm_api_base || ''
  form.llm_api_key = c.llm_api_key || ''
  form.llm_model = c.llm_model || ''
  form.llm_timeout = c.llm_timeout ?? 120
  form.llm_max_concurrency = c.llm_max_concurrency ?? 4
  form.embed_backend = c.embed_backend || 'mock'
  form.embed_api_base = c.embed_api_base || ''
  form.embed_api_key = c.embed_api_key || ''
  form.embed_model = c.embed_model || ''
  form.embed_timeout = c.embed_timeout ?? 60
  form.pdf_render_dpi = c.pdf_render_dpi ?? 200
  keyDirty.value = false
  embedKeyDirty.value = false
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

function onEmbedKeyInput(): void {
  embedKeyDirty.value = true
}

async function onSave(): Promise<void> {
  saveError.value = null
  saved.value = false
  const payload: Record<string, unknown> = {
    llm_api_base: form.llm_api_base.trim(),
    llm_model: form.llm_model.trim(),
    llm_timeout: Number(form.llm_timeout),
    llm_max_concurrency: Number(form.llm_max_concurrency),
    pdf_render_dpi: Number(form.pdf_render_dpi),
    embed_backend: form.embed_backend,
  }
  payload.llm_api_key = keyDirty.value ? form.llm_api_key : '********'

  // qwen backend 才提交向量服务字段(mock 无需配置)
  if (form.embed_backend === 'qwen') {
    payload.embed_api_base = form.embed_api_base.trim()
    payload.embed_model = form.embed_model.trim()
    payload.embed_timeout = Number(form.embed_timeout)
    payload.embed_api_key = embedKeyDirty.value ? form.embed_api_key : '********'
  }

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
</script>

<template>
  <div class="model-config">
    <div v-if="store.loading" class="muted">加载配置中…</div>

    <section class="card">
      <h2 class="page-title">OCR 引擎</h2>
      <p class="muted page-desc">
        通过远端多模态推理服务(vLLM / SGLang / 云端 API)识别 PDF 扫描件。
        所有模型(PaddleOCR-VL / 通义千问 VL / GPT-4o 等)共用下方同一套 API 配置,按模型名路由到对应服务。
      </p>
    </section>

    <section class="card">
      <h3 class="section-title">推理服务配置</h3>

      <div class="form-grid">
        <div class="field">
          <label>API Base</label>
          <input v-model="form.llm_api_base" class="input" placeholder="http://localhost:8000/v1" />
          <span class="hint">vLLM / SGLang / 云端 API 的 OpenAI 兼容根地址</span>
        </div>

        <div class="field">
          <label>模型名称</label>
          <input v-model="form.llm_model" class="input" placeholder="qwen-vl-max" />
          <span class="hint">加载的模型名(决定走哪个推理服务)</span>
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

        <div class="field">
          <label>渲染 DPI</label>
          <input v-model.number="form.pdf_render_dpi" class="input" type="number" min="72" max="600" />
          <span class="hint">PDF 渲染为图片的分辨率;200 为速度/质量甜点(默认),过高会显著变慢</span>
        </div>
      </div>
    </section>

    <section class="card">
      <h2 class="page-title">语义向量引擎</h2>
      <p class="muted page-desc">
        用于条款对齐(无编号时的语义匹配)与篡改判定的相似度计算。
        mock 仅反映字面重叠(测试用);qwen 走 OpenAI 兼容的 /v1/embeddings,
        适合生产环境自建 Qwen3-Embedding。
      </p>

      <div class="field backend-field">
        <label>引擎</label>
        <div class="radio-row">
          <label class="radio">
            <input type="radio" value="mock" v-model="form.embed_backend" />
            <span>mock(字面相似度)</span>
          </label>
          <label class="radio">
            <input type="radio" value="qwen" v-model="form.embed_backend" />
            <span>qwen(语义嵌入 API)</span>
          </label>
        </div>
      </div>

      <div v-if="form.embed_backend === 'qwen'" class="form-grid">
        <div class="field">
          <label>API Base</label>
          <input v-model="form.embed_api_base" class="input" placeholder="http://vllm-embed:8000/v1" />
          <span class="hint">向量服务的 OpenAI 兼容根地址(独立于 OCR)</span>
        </div>

        <div class="field">
          <label>模型名称</label>
          <input v-model="form.embed_model" class="input" placeholder="Qwen3-Embedding-0.6B" />
          <span class="hint">部署的 embedding 模型名</span>
        </div>

        <div class="field span-2">
          <label>API Key</label>
          <input
            v-model="form.embed_api_key"
            class="input"
            type="password"
            :placeholder="store.config?.embed_api_key_set ? '已设置(输入新值覆盖)' : '本地 vLLM 通常留空'"
            @input="onEmbedKeyInput"
          />
          <span class="hint">
            <template v-if="store.config?.embed_api_key_set">
              当前: {{ store.config.embed_api_key || '****' }} · 留空不修改
            </template>
            <template v-else>本地 vLLM 一般无需 Key</template>
          </span>
        </div>

        <div class="field">
          <label>请求超时(秒)</label>
          <input v-model.number="form.embed_timeout" class="input" type="number" min="5" max="300" />
          <span class="hint">单次向量请求的超时上限</span>
        </div>
      </div>
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

.form-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 14px;
}
.span-2 {
  grid-column: span 2;
}
@media (max-width: 640px) {
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
.backend-field {
  margin-bottom: 16px;
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
