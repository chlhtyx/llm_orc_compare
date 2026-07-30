<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue'
import type { LlmConfig } from '@/api/config'
import { useModelConfigStore } from '@/stores/modelConfig'

const configStore = useModelConfigStore()
const keyDirty = ref(false)
const saved = ref(false)
const saveError = ref<string | null>(null)

const form = reactive({
  external_api_key: '',
  external_public_base_url: '',
  external_max_upload_mb: 50,
  external_image_dpi: 144,
  external_ocr_backend: 'paddleocr' as 'llm' | 'paddleocr',
})

const keyPendingClear = computed(
  () => keyDirty.value && form.external_api_key === '' && !!configStore.config?.external_api_key_set,
)

function syncFromConfig(config: LlmConfig | null): void {
  if (!config) return
  // 不回填脱敏 Key，避免把掩码保存为真实凭据。
  form.external_api_key = ''
  form.external_public_base_url = config.external_public_base_url || ''
  form.external_max_upload_mb = config.external_max_upload_mb ?? 50
  form.external_image_dpi = config.external_image_dpi ?? 144
  form.external_ocr_backend = config.external_ocr_backend || 'paddleocr'
  keyDirty.value = false
}

onMounted(async () => {
  await configStore.fetch()
  syncFromConfig(configStore.config)
  if (configStore.error) saveError.value = configStore.error
})

watch(() => configStore.config, (config) => syncFromConfig(config))

async function onSave(): Promise<void> {
  saved.value = false
  saveError.value = null
  const ok = await configStore.save({
    external_api_key: keyDirty.value ? form.external_api_key : '********',
    external_public_base_url: form.external_public_base_url.trim(),
    external_max_upload_mb: Number(form.external_max_upload_mb),
    external_image_dpi: Number(form.external_image_dpi),
    external_ocr_backend: form.external_ocr_backend,
  })
  if (!ok) {
    saveError.value = configStore.error
    return
  }
  saved.value = true
  window.setTimeout(() => (saved.value = false), 2500)
}

async function onReset(): Promise<void> {
  await configStore.fetch()
  syncFromConfig(configStore.config)
  saveError.value = configStore.error
  saved.value = false
}

function onClearKey(): void {
  form.external_api_key = ''
  keyDirty.value = true
}
</script>

<template>
  <div class="api-config">
    <header class="page-header">
      <div>
        <p class="eyebrow">External API</p>
        <h1>外部 API 配置</h1>
        <p>集中维护合同比对 API 与金额统计 API 共用的访问入口、鉴权和默认处理资源。</p>
      </div>
      <div class="service-state" :class="{ enabled: configStore.config?.external_enabled }">
        <span class="state-dot" />
        <div><small>服务状态</small><strong>{{ configStore.config?.external_enabled ? '可调用' : '待配置' }}</strong></div>
      </div>
    </header>

    <p v-if="configStore.loading" class="loading-state">正在读取外部 API 配置…</p>

    <section class="card config-card">
      <div class="section-heading">
        <div>
          <span class="section-index">01</span>
          <h2>通用访问配置</h2>
          <p>保存后同时应用于 <code>contractCompare</code> 与 <code>amountStat</code>。</p>
        </div>
      </div>

      <div class="form-grid">
        <div class="field span-2">
          <label for="external-api-key">外部调用 API Key</label>
          <input
            id="external-api-key"
            v-model="form.external_api_key"
            class="input"
            type="password"
            autocomplete="new-password"
            :placeholder="configStore.config?.external_api_key_set ? '已设置，输入新值可覆盖' : '可选：留空则不鉴权'"
            @input="keyDirty = true"
          />
          <span class="hint">
            <template v-if="configStore.config?.external_api_key_set">
              当前值：{{ configStore.config.external_api_key || '****' }}；不修改即可保留。
              <button v-if="!keyPendingClear" type="button" class="link-btn danger" @click="onClearKey">清除当前 Key</button>
              <span v-else class="pending-clear">将清除当前 Key（不鉴权直接放行）</span>
            </template>
            <template v-else>两个 API 都通过 <code>X-API-Key</code> 请求头鉴权。</template>
          </span>
        </div>

        <div class="field span-2">
          <label for="external-base-url">服务公开地址</label>
          <input id="external-base-url" v-model="form.external_public_base_url" class="input" placeholder="https://compare.example.com" />
          <span class="hint">用于生成外部结果查询链接；不要包含查询参数或 # 片段。</span>
        </div>

        <div class="field">
          <label for="upload-limit">单文件上传上限</label>
          <div class="input-with-unit">
            <input id="upload-limit" v-model.number="form.external_max_upload_mb" class="input" type="number" min="1" max="1024" />
            <span>MiB</span>
          </div>
          <span class="hint">限制每个上传文件和每个 URL 下载文件。</span>
        </div>

        <div class="field">
          <label for="image-dpi">合同高亮图片 DPI</label>
          <div class="input-with-unit">
            <input id="image-dpi" v-model.number="form.external_image_dpi" class="input" type="number" min="72" max="600" />
            <span>DPI</span>
          </div>
          <span class="hint">合同比对 API 生成逐页高亮 PNG 时使用；服务启用状态依赖有效值。</span>
        </div>
      </div>

      <div class="engine-panel">
        <div><h3>共用 OCR 默认引擎</h3><p>当前两个外部 API 共用此默认值，变更会同时影响两条 API 管线。</p></div>
        <fieldset class="engine-field">
          <legend>识别引擎</legend>
          <label class="choice" :class="{ selected: form.external_ocr_backend === 'paddleocr' }">
            <input v-model="form.external_ocr_backend" type="radio" value="paddleocr" />
            <span><strong>PaddleOCR</strong><small>专用 OCR 模型</small></span>
          </label>
          <label class="choice" :class="{ selected: form.external_ocr_backend === 'llm' }">
            <input v-model="form.external_ocr_backend" type="radio" value="llm" />
            <span><strong>LLM</strong><small>通用视觉语言模型</small></span>
          </label>
        </fieldset>
      </div>

      <div class="actions">
        <button class="btn btn-primary" :disabled="configStore.saving" @click="onSave">{{ configStore.saving ? '保存中…' : '保存通用配置' }}</button>
        <button class="btn" type="button" :disabled="configStore.loading" @click="onReset">恢复已保存配置</button>
        <span v-if="saved" class="save-success">配置已生效</span>
      </div>
      <p v-if="saveError" class="err">{{ saveError }}</p>
    </section>
  </div>
</template>

<style scoped>
.api-config { max-width: 900px; margin: 0 auto; }
.page-header { display: flex; align-items: flex-end; justify-content: space-between; gap: 28px; padding: 10px 2px 28px; }
.eyebrow, .section-index { margin: 0 0 8px; color: var(--primary); font-family: var(--mono); font-size: 11px; font-weight: 700; letter-spacing: .12em; text-transform: uppercase; }
.page-header h1 { margin: 0; font-size: clamp(28px, 4vw, 40px); letter-spacing: -.035em; }
.page-header p:not(.eyebrow) { max-width: 640px; margin: 10px 0 0; color: var(--text-muted); }
.service-state { display: flex; flex: 0 0 auto; align-items: center; gap: 10px; padding: 10px 13px; border: 1px solid var(--border); border-radius: var(--radius-sm); background: var(--surface); }
.state-dot { width: 8px; height: 8px; border-radius: 50%; background: var(--risk-medium); box-shadow: 0 0 0 4px var(--risk-medium-bg); }
.service-state.enabled .state-dot { background: var(--risk-clean); box-shadow: 0 0 0 4px var(--risk-low-bg); }
.service-state small, .service-state strong { display: block; line-height: 1.35; }.service-state small { color: var(--text-muted); font-size: 11px; }.service-state strong { font-size: 13px; }
.loading-state, .section-heading p, .engine-panel p { color: var(--text-muted); }.loading-state { margin: 0 0 12px; }
.config-card { padding: 26px 28px 30px; }.section-heading { margin-bottom: 24px; }.section-index { display: block; margin-bottom: 4px; }.section-heading h2 { margin: 0; font-size: 20px; }.section-heading p { margin: 5px 0 0; }
.form-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 18px; }.span-2 { grid-column: span 2; }
.input-with-unit { position: relative; }.input-with-unit .input { padding-right: 52px; font-variant-numeric: tabular-nums; }.input-with-unit > span { position: absolute; top: 50%; right: 10px; color: var(--text-muted); font-size: 11px; transform: translateY(-50%); }
.hint code { font-family: var(--mono); }.hint .link-btn { margin-left: 6px; padding: 0; border: 0; color: var(--primary); background: none; font: inherit; font-size: 12px; text-decoration: underline; cursor: pointer; }.hint .link-btn.danger, .hint .pending-clear { color: var(--risk-high); }.hint .pending-clear { margin-left: 6px; font-size: 12px; }
.engine-panel { display: grid; grid-template-columns: .8fr 1.2fr; gap: 24px; align-items: center; margin-top: 26px; padding: 20px; border-radius: var(--radius-sm); background: var(--surface-2); }.engine-panel h3 { margin: 0; font-size: 15px; }.engine-panel p { margin: 5px 0 0; font-size: 12px; }
.engine-field { display: flex; gap: 8px; margin: 0; padding: 0; border: 0; }.engine-field legend { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); }.choice { display: flex; flex: 1; align-items: center; gap: 8px; padding: 9px 10px; border: 1px solid transparent; border-radius: 5px; background: var(--surface); cursor: pointer; }.choice.selected { border-color: color-mix(in srgb, var(--primary) 55%, var(--border)); }.choice span, .choice strong, .choice small { display: block; }.choice strong { font-size: 12px; }.choice small { color: var(--text-muted); font-size: 10px; }
.actions { display: flex; align-items: center; gap: 10px; margin-top: 22px; }.save-success { color: var(--risk-clean); font-size: 13px; font-weight: 600; }.err { margin: 8px 0 0; color: var(--risk-high); font-size: 13px; }
@media (max-width: 700px) { .page-header { align-items: flex-start; flex-direction: column; }.engine-panel { grid-template-columns: 1fr; } }
@media (max-width: 520px) { .config-card { padding: 20px; }.form-grid { grid-template-columns: 1fr; }.span-2 { grid-column: span 1; }.engine-field { flex-direction: column; } }
</style>
