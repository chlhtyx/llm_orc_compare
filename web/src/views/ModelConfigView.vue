<script setup lang="ts">
import { onMounted, reactive, ref, watch } from 'vue'
import { useModelConfigStore } from '@/stores/modelConfig'
import type { LlmConfig } from '@/api/config'

const store = useModelConfigStore()

const form = reactive({
  // —— llm 引擎(通用 VL 模型)——
  llm_api_base: '',
  llm_api_key: '',
  llm_model: '',
  llm_timeout: 120,
  llm_max_concurrency: 4,
  // —— paddleocr 引擎(专用 OCR 模型)——
  paddleocr_api_mode: 'vllm' as 'vllm' | 'official_sdk' | 'paddlex_serving',
  paddleocr_api_base: '',
  paddleocr_api_key: '',
  paddleocr_model: '',
  paddleocr_official_api_base: '',
  paddleocr_official_access_token: '',
  paddleocr_official_model: 'PaddleOCR-VL-1.6',
  paddleocr_paddlex_api_base: '',
  paddleocr_paddlex_endpoint: '/ocr',
  paddleocr_paddlex_api_key: '',
  paddleocr_timeout: 300,
  paddleocr_max_concurrency: 4,
  // —— LLM 辅助说明服务 ——
  judge_api_base: '',
  judge_api_key: '',
  judge_model: '',
  judge_timeout: 120,
  // —— 语义向量引擎 ——
  embed_backend: 'mock',
  embed_api_base: '',
  embed_api_key: '',
  embed_model: '',
  embed_timeout: 60,
  pdf_render_dpi: 200,
  max_pdf_pages: 0,
})

const keyDirty = ref(false)
const paddleKeyDirty = ref(false)
const paddleOfficialTokenDirty = ref(false)
const paddlePaddlexKeyDirty = ref(false)
const embedKeyDirty = ref(false)
const judgeKeyDirty = ref(false)
const saved = ref(false)
const saveError = ref<string | null>(null)

function syncFromConfig(c: LlmConfig | null): void {
  if (!c) return
  form.llm_api_base = c.llm_api_base || ''
  form.llm_api_key = c.llm_api_key || ''
  form.llm_model = c.llm_model || ''
  form.llm_timeout = c.llm_timeout ?? 120
  form.llm_max_concurrency = c.llm_max_concurrency ?? 4
  form.paddleocr_api_mode = c.paddleocr_api_mode || 'vllm'
  form.paddleocr_api_base = c.paddleocr_api_base || ''
  form.paddleocr_api_key = c.paddleocr_api_key || ''
  form.paddleocr_model = c.paddleocr_model || ''
  form.paddleocr_official_api_base = c.paddleocr_official_api_base || ''
  form.paddleocr_official_access_token = c.paddleocr_official_access_token || ''
  form.paddleocr_official_model = c.paddleocr_official_model || 'PaddleOCR-VL-1.6'
  form.paddleocr_paddlex_api_base = c.paddleocr_paddlex_api_base || ''
  form.paddleocr_paddlex_endpoint = c.paddleocr_paddlex_endpoint || '/ocr'
  form.paddleocr_paddlex_api_key = c.paddleocr_paddlex_api_key || ''
  form.paddleocr_timeout = c.paddleocr_timeout ?? 300
  form.paddleocr_max_concurrency = c.paddleocr_max_concurrency ?? 4
  form.judge_api_base = c.judge_api_base || ''
  form.judge_api_key = c.judge_api_key || ''
  form.judge_model = c.judge_model || ''
  form.judge_timeout = c.judge_timeout ?? 120
  form.embed_backend = c.embed_backend || 'mock'
  form.embed_api_base = c.embed_api_base || ''
  form.embed_api_key = c.embed_api_key || ''
  form.embed_model = c.embed_model || ''
  form.embed_timeout = c.embed_timeout ?? 60
  form.pdf_render_dpi = c.pdf_render_dpi ?? 200
  form.max_pdf_pages = c.max_pdf_pages ?? 0
  keyDirty.value = false
  paddleKeyDirty.value = false
  paddleOfficialTokenDirty.value = false
  paddlePaddlexKeyDirty.value = false
  embedKeyDirty.value = false
  judgeKeyDirty.value = false
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

function onPaddleKeyInput(): void {
  paddleKeyDirty.value = true
}

function onPaddleOfficialTokenInput(): void {
  paddleOfficialTokenDirty.value = true
}

function onPaddlePaddlexKeyInput(): void {
  paddlePaddlexKeyDirty.value = true
}

function onEmbedKeyInput(): void {
  embedKeyDirty.value = true
}

function onJudgeKeyInput(): void {
  judgeKeyDirty.value = true
}

async function onSave(): Promise<void> {
  saveError.value = null
  saved.value = false
  const payload: Record<string, unknown> = {
    // —— llm 引擎 ——
    llm_api_base: form.llm_api_base.trim(),
    llm_model: form.llm_model.trim(),
    llm_timeout: Number(form.llm_timeout),
    llm_max_concurrency: Number(form.llm_max_concurrency),
    llm_api_key: keyDirty.value ? form.llm_api_key : '********',
    // —— paddleocr 引擎(独立于 llm,填了才提交)——
    paddleocr_api_mode: form.paddleocr_api_mode,
    paddleocr_api_base: form.paddleocr_api_base.trim(),
    paddleocr_model: form.paddleocr_model.trim(),
    paddleocr_timeout: Number(form.paddleocr_timeout),
    paddleocr_max_concurrency: Number(form.paddleocr_max_concurrency),
    paddleocr_api_key: paddleKeyDirty.value ? form.paddleocr_api_key : '********',
    paddleocr_official_api_base: form.paddleocr_official_api_base.trim(),
    paddleocr_official_model: form.paddleocr_official_model.trim(),
    paddleocr_official_access_token: paddleOfficialTokenDirty.value
      ? form.paddleocr_official_access_token
      : '********',
    paddleocr_paddlex_api_base: form.paddleocr_paddlex_api_base.trim(),
    paddleocr_paddlex_endpoint: form.paddleocr_paddlex_endpoint.trim() || '/ocr',
    paddleocr_paddlex_api_key: paddlePaddlexKeyDirty.value
      ? form.paddleocr_paddlex_api_key
      : '********',
    // —— 全局 ——
    pdf_render_dpi: Number(form.pdf_render_dpi),
    max_pdf_pages: Number(form.max_pdf_pages),
    embed_backend: form.embed_backend,
  }

  // LLM 辅助说明服务(独立于 OCR,填了才提交)
  if (form.judge_api_base.trim() || form.judge_model.trim()) {
    payload.judge_api_base = form.judge_api_base.trim()
    payload.judge_model = form.judge_model.trim()
    payload.judge_timeout = Number(form.judge_timeout)
    payload.judge_api_key = judgeKeyDirty.value ? form.judge_api_key : '********'
  }

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
      <h2 class="page-title">llm 引擎(通用 VL 模型)</h2>
      <p class="muted page-desc">
        识别 PDF 扫描件版面的多模态 VL 模型。兼容 OpenAI Chat Completions 协议,
        适配能返回结构化 JSON 的通用对话 VL 模型(Qwen-VL-Max / Qwen3-VL / GPT-4o 等)。
        在“合同比对 API”页选择「llm」或其他流程指定该引擎时使用本配置。
      </p>

      <div class="form-grid">
        <div class="field">
          <label>API Base</label>
          <input v-model="form.llm_api_base" class="input" placeholder="https://dashscope.aliyuncs.com/compatible-mode/v1" />
          <span class="hint">OpenAI 兼容根地址(vLLM / SGLang / SiliconFlow / DashScope 等均可)</span>
        </div>

        <div class="field">
          <label>模型名称</label>
          <input v-model="form.llm_model" class="input" placeholder="qwen-vl-max" />
          <span class="hint">通用 VL 模型(如 qwen-vl-max / Qwen3-VL-32B-Instruct)</span>
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

    <section class="card">
      <h2 class="page-title">paddleocr 引擎(专用 OCR 模型)</h2>
      <p class="muted page-desc">
        可使用原有 vLLM / OpenAI 兼容服务，也可使用 PaddleOCR 官方同步 API 或 Python SDK
        调用 AI Studio 托管 API，或调用自建 PaddleX serving 服务。
        官方 SDK 会直接返回分页 Markdown 和版面坐标；
        vLLM 模式保留现有 OCR + Spotting 调用。<b>配置完全独立</b>于 llm 引擎。
        在“合同比对 API”页选择「paddleocr」或其他流程指定该引擎时使用本配置。
      </p>

      <div class="form-grid">
        <div class="field span-2 backend-field">
          <label>调用方式</label>
          <div class="radio-row">
            <label class="radio">
              <input v-model="form.paddleocr_api_mode" type="radio" value="vllm" />
              <span>vLLM / OpenAI 兼容(原有)</span>
            </label>
            <label class="radio">
              <input v-model="form.paddleocr_api_mode" type="radio" value="official_sdk" />
              <span>PaddleOCR 官方 API / SDK</span>
            </label>
            <label class="radio">
              <input v-model="form.paddleocr_api_mode" type="radio" value="paddlex_serving" />
              <span>自建 PaddleX Serving</span>
            </label>
          </div>
        </div>

        <div v-if="form.paddleocr_api_mode === 'vllm'" class="field">
          <label>API Base</label>
          <input v-model="form.paddleocr_api_base" class="input" placeholder="https://api.siliconflow.cn/v1" />
          <span class="hint">OpenAI 兼容根地址(必填)</span>
        </div>

        <div v-if="form.paddleocr_api_mode === 'vllm'" class="field">
          <label>模型名称</label>
          <input v-model="form.paddleocr_model" class="input" placeholder="PaddlePaddle/PaddleOCR-VL-1.5" />
          <span class="hint">vLLM 服务暴露的模型名</span>
        </div>

        <div v-if="form.paddleocr_api_mode === 'vllm'" class="field span-2">
          <label>API Key</label>
          <input
            v-model="form.paddleocr_api_key"
            class="input"
            type="password"
            :placeholder="store.config?.paddleocr_api_key_set ? '已设置(输入新值覆盖)' : '本地 vLLM 通常留空'"
            @input="onPaddleKeyInput"
          />
          <span class="hint">
            <template v-if="store.config?.paddleocr_api_key_set">
              当前: {{ store.config.paddleocr_api_key || '****' }} · 留空不修改
            </template>
            <template v-else>本地 vLLM 一般无需 Key;云端服务需要</template>
          </span>
        </div>

        <div v-if="form.paddleocr_api_mode === 'official_sdk'" class="field">
          <label>官方 API 地址</label>
          <input
            v-model="form.paddleocr_official_api_base"
            class="input"
            placeholder="https://...aistudio-app.com/layout-parsing"
          />
          <span class="hint">
            填写完整 /layout-parsing 地址时走网页端同步 API；留空时走异步任务 SDK
          </span>
        </div>

        <div v-if="form.paddleocr_api_mode === 'official_sdk'" class="field">
          <label>官方模型</label>
          <input
            v-model="form.paddleocr_official_model"
            class="input"
            placeholder="PaddleOCR-VL-1.6"
          />
          <span class="hint">支持 PaddleOCR-VL / PaddleOCR-VL-1.5 / PaddleOCR-VL-1.6</span>
        </div>

        <div v-if="form.paddleocr_api_mode === 'official_sdk'" class="field span-2">
          <label>AI Studio Access Token</label>
          <input
            v-model="form.paddleocr_official_access_token"
            class="input"
            type="password"
            :placeholder="store.config?.paddleocr_official_access_token_set ? '已设置(输入新值覆盖)' : 'Access Token(必填)'"
            @input="onPaddleOfficialTokenInput"
          />
          <span class="hint">
            <template v-if="store.config?.paddleocr_official_access_token_set">
              当前: {{ store.config.paddleocr_official_access_token || '****' }} · 留空不修改
            </template>
            <template v-else>从 PaddleOCR 官方 AI Studio 获取</template>
          </span>
        </div>

        <div v-if="form.paddleocr_api_mode === 'paddlex_serving'" class="field">
          <label>API Base</label>
          <input
            v-model="form.paddleocr_paddlex_api_base"
            class="input"
            placeholder="http://192.168.100.102:8080"
          />
          <span class="hint">自建 PaddleX serving 根地址(必填,不含端点路径)</span>
        </div>

        <div v-if="form.paddleocr_api_mode === 'paddlex_serving'" class="field">
          <label>端点路径</label>
          <input
            v-model="form.paddleocr_paddlex_endpoint"
            class="input"
            placeholder="/ocr"
          />
          <span class="hint">
            /ocr = 通用 OCR 产线(只有文本行,无版面结构);
            /layout-parsing = PP-StructureV3 版面解析产线(含段落/表格结构)
          </span>
        </div>

        <div v-if="form.paddleocr_api_mode === 'paddlex_serving'" class="field span-2">
          <label>API Key(可选)</label>
          <input
            v-model="form.paddleocr_paddlex_api_key"
            class="input"
            type="password"
            :placeholder="store.config?.paddleocr_paddlex_api_key_set ? '已设置(输入新值覆盖)' : '自建服务通常留空'"
            @input="onPaddlePaddlexKeyInput"
          />
          <span class="hint">
            <template v-if="store.config?.paddleocr_paddlex_api_key_set">
              当前: {{ store.config.paddleocr_paddlex_api_key || '****' }} · 留空不修改
            </template>
            <template v-else>自建 PaddleX serving 默认无鉴权,仅服务端开启鉴权时填写</template>
          </span>
        </div>

        <div class="field">
          <label>请求超时(秒)</label>
          <input v-model.number="form.paddleocr_timeout" class="input" type="number" min="10" max="3600" />
          <span class="hint">
            {{ form.paddleocr_api_mode === 'official_sdk'
              ? '同步 API 的请求超时；异步 SDK 总轮询不少于 900 秒'
              : form.paddleocr_api_mode === 'paddlex_serving'
                ? '整本 PDF 一次提交的请求超时'
                : '单页识别的超时上限' }}
          </span>
        </div>

        <div class="field">
          <label>最大并发</label>
          <input v-model.number="form.paddleocr_max_concurrency" class="input" type="number" min="1" max="32" />
          <span class="hint">逐页并行数,受推理服务限流</span>
        </div>
      </div>
    </section>

    <section class="card">
      <h3 class="section-title">共享设置</h3>
      <div class="form-grid">
        <div class="field">
          <label>渲染 DPI</label>
          <input v-model.number="form.pdf_render_dpi" class="input" type="number" min="72" max="600" />
          <span class="hint">PDF 渲染为图片的分辨率,两种引擎共用;200 为速度/质量甜点(默认),过高会显著变慢</span>
        </div>

        <div class="field">
          <label>最大页数</label>
          <input v-model.number="form.max_pdf_pages" class="input" type="number" min="0" />
          <span class="hint">提交时若扫描件 PDF 页数超过此值,直接拒绝(暂不支持),不进入比对流程;0 表示不限制</span>
        </div>
      </div>
    </section>

    <section class="card">
      <h2 class="page-title">LLM 联合分段对齐与辅助说明服务</h2>
      <p class="muted page-desc">
        同一纯文本 LLM 可基于 DOCX 段落和 PDF OCR 块的字符区间联合分段与对齐，
        也可对已确认「modified」条款补充严重度建议和解释；
        联合计划必须通过全覆盖、无重叠和单调校验，LLM 不得撤销变化或降低规则下限。
        需<b>纯文本 LLM</b>(如 Qwen3.5),与 OCR 的多模态 VL 模型分开配置。
        “合同比对 API”页分别启用联合对齐或辅助说明后生效；未配置时回退原 Clause 向量与规则链路。
      </p>

      <div class="form-grid">
        <div class="field">
          <label>API Base</label>
          <input v-model="form.judge_api_base" class="input" placeholder="https://api.siliconflow.cn/v1" />
          <span class="hint">OpenAI 兼容根地址(可与 OCR 服务相同或不同)</span>
        </div>

        <div class="field">
          <label>模型名称</label>
          <input v-model="form.judge_model" class="input" placeholder="Qwen/Qwen3.5-35B-A3B" />
          <span class="hint">纯文本 LLM 模型名(不要填 VL 模型)</span>
        </div>

        <div class="field span-2">
          <label>API Key</label>
          <input
            v-model="form.judge_api_key"
            class="input"
            type="password"
            :placeholder="store.config?.judge_api_key_set ? '已设置(输入新值覆盖)' : '与 OCR 服务相同时可填同一 Key'"
            @input="onJudgeKeyInput"
          />
          <span class="hint">
            <template v-if="store.config?.judge_api_key_set">
              当前: {{ store.config.judge_api_key || '****' }} · 留空不修改
            </template>
            <template v-else>未配置则沿用规则结论</template>
          </span>
        </div>

        <div class="field">
          <label>请求超时(秒)</label>
          <input v-model.number="form.judge_timeout" class="input" type="number" min="10" max="600" />
          <span class="hint">单条条款复核的超时上限</span>
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
</style>
