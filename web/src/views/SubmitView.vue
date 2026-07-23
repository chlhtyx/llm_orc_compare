<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue'
import FileDrop from '@/components/FileDrop.vue'
import CompareApiPipelineTest from '@/components/CompareApiPipelineTest.vue'
import type { LlmConfig } from '@/api/config'
import { useModelConfigStore } from '@/stores/modelConfig'

const configStore = useModelConfigStore()

const sourceFile = ref<File | null>(null)
const targetFile = ref<File | null>(null)
const keyDirty = ref(false)
const saved = ref(false)
const saveError = ref<string | null>(null)

const form = reactive({
  external_api_key: '',
  external_public_base_url: '',
  external_max_upload_mb: 50,
  external_image_dpi: 144,
  external_ocr_backend: 'paddleocr' as 'llm' | 'paddleocr',
  external_enable_llm_judge: false,
  external_enable_risk_assessment: false,
  external_truncate_to_original_pages: false,
})

const sourceValid = computed(() => !!sourceFile.value?.name.toLowerCase().endsWith('.docx'))
const targetValid = computed(() => !!targetFile.value?.name.toLowerCase().endsWith('.pdf'))
const endpoint = computed(() => {
  const base = form.external_public_base_url.trim().replace(/\/$/, '')
  return `${base || 'https://compare.example.com'}/api/v1/external/contractCompare`
})

function syncFromConfig(config: LlmConfig | null): void {
  if (!config) return
  // 不把脱敏值放回可编辑框，避免用户编辑星号后误覆盖真实 Key。
  form.external_api_key = ''
  form.external_public_base_url = config.external_public_base_url || ''
  form.external_max_upload_mb = config.external_max_upload_mb ?? 50
  form.external_image_dpi = config.external_image_dpi ?? 144
  form.external_ocr_backend = config.external_ocr_backend || 'paddleocr'
  form.external_enable_llm_judge = config.external_enable_llm_judge ?? false
  form.external_enable_risk_assessment = config.external_enable_risk_assessment ?? false
  form.external_truncate_to_original_pages = config.external_truncate_to_original_pages ?? false
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
    external_enable_llm_judge:
      form.external_enable_risk_assessment && form.external_enable_llm_judge,
    external_enable_risk_assessment: form.external_enable_risk_assessment,
    external_truncate_to_original_pages: form.external_truncate_to_original_pages,
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
</script>

<template>
  <div class="api-workbench">
    <header class="workbench-header">
      <div>
        <p class="eyebrow">Contract comparison API</p>
        <h1>合同比对 API</h1>
        <p class="header-copy">
          配置外部系统调用入口，并用真实合同验证从识别、比对到高亮图片的完整管线。
        </p>
      </div>
      <div class="service-state" :class="{ enabled: configStore.config?.external_enabled }">
        <span class="state-dot" />
        <div>
          <small>服务状态</small>
          <strong>{{ configStore.config?.external_enabled ? '可调用' : '待配置' }}</strong>
        </div>
      </div>
    </header>

    <p v-if="configStore.loading" class="loading-state">正在读取 API 配置…</p>

    <section class="card config-card">
      <div class="section-heading">
        <div>
          <span class="section-index">01</span>
          <h2>API 服务配置</h2>
          <p>保存后立即应用于正式外部接口和下方管线测试，无需重启服务。</p>
        </div>
        <code>POST {{ endpoint }}</code>
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
            :placeholder="configStore.config?.external_api_key_set ? '已设置，输入新值可覆盖' : '请输入高强度随机 Key'"
            @input="keyDirty = true"
          />
          <span class="hint">
            <template v-if="configStore.config?.external_api_key_set">
              当前值：{{ configStore.config.external_api_key || '****' }}；不修改此输入框即可保留原 Key。
            </template>
            <template v-else>外部系统通过 <code>X-API-Key</code> 请求头鉴权。</template>
          </span>
        </div>

        <div class="field span-2">
          <label for="external-base-url">服务公开地址</label>
          <input
            id="external-base-url"
            v-model="form.external_public_base_url"
            class="input"
            placeholder="https://compare.example.com"
          />
          <span class="hint">用于生成结果查询地址和高亮 PNG 绝对链接，不要包含查询参数或 # 片段。</span>
        </div>

        <div class="field">
          <label for="upload-limit">单文件上传上限</label>
          <div class="input-with-unit">
            <input
              id="upload-limit"
              v-model.number="form.external_max_upload_mb"
              class="input"
              type="number"
              min="1"
              max="1024"
            />
            <span>MiB</span>
          </div>
          <span class="hint">分别限制原始 DOCX 和回收 PDF。</span>
        </div>

        <div class="field">
          <label for="image-dpi">高亮图片清晰度</label>
          <div class="input-with-unit">
            <input
              id="image-dpi"
              v-model.number="form.external_image_dpi"
              class="input"
              type="number"
              min="72"
              max="600"
            />
            <span>DPI</span>
          </div>
          <span class="hint">用于生成整份回收件的逐页 PNG。</span>
        </div>
      </div>

      <div class="options-panel">
        <div class="options-copy">
          <h3>比对选项 <span>可选</span></h3>
          <p>作为 API 的默认比对配置，正式调用与测试任务保持一致。</p>
        </div>

        <fieldset class="engine-field">
          <legend>识别引擎</legend>
          <label class="choice" :class="{ selected: form.external_ocr_backend === 'paddleocr' }">
            <input v-model="form.external_ocr_backend" type="radio" value="paddleocr" />
            <span>
              <strong>PaddleOCR</strong>
              <small>专用 OCR 模型</small>
            </span>
          </label>
          <label class="choice" :class="{ selected: form.external_ocr_backend === 'llm' }">
            <input v-model="form.external_ocr_backend" type="radio" value="llm" />
            <span>
              <strong>LLM</strong>
              <small>通用视觉语言模型</small>
            </span>
          </label>
        </fieldset>

        <div class="option-toggles">
          <label class="toggle-row">
            <span>
              <strong>风险评估</strong>
              <small>开启高、中、低风险分级与高风险要素抽取。</small>
            </span>
            <input v-model="form.external_enable_risk_assessment" type="checkbox" role="switch" />
          </label>
          <label class="toggle-row" :class="{ disabled: !form.external_enable_risk_assessment }">
            <span>
              <strong>LLM 辅助说明</strong>
              <small>依赖风险评估；只补充解释，不会撤销确定变化。</small>
            </span>
            <input
              v-model="form.external_enable_llm_judge"
              type="checkbox"
              role="switch"
              :disabled="!form.external_enable_risk_assessment"
            />
          </label>
          <label class="toggle-row">
            <span>
              <strong>回收件页数截取</strong>
              <small>回收 PDF 超过原始合同页数时，自动截取前 N 页再比对。</small>
            </span>
            <input
              v-model="form.external_truncate_to_original_pages"
              type="checkbox"
              role="switch"
            />
          </label>
        </div>
      </div>

      <div class="config-actions">
        <button class="btn btn-primary" :disabled="configStore.saving" @click="onSave">
          {{ configStore.saving ? '保存中…' : '保存 API 配置' }}
        </button>
        <button class="btn" type="button" :disabled="configStore.loading" @click="onReset">
          恢复已保存配置
        </button>
        <span v-if="saved" class="save-success">配置已生效</span>
      </div>
      <p v-if="saveError" class="err">{{ saveError }}</p>
    </section>

    <section class="card test-card">
      <div class="section-heading">
        <div>
          <span class="section-index">02</span>
          <h2>API 管线测试</h2>
          <p>上传一组真实文件，测试任务不会向外部系统发送回调。</p>
        </div>
      </div>

      <div class="file-grid">
        <div class="field">
          <label>原始合同（Word）</label>
          <FileDrop
            v-model="sourceFile"
            accept=".docx"
            label="选择 .docx 文件"
            hint="作为比对基准的原始合同"
          />
          <span v-if="sourceFile && !sourceValid" class="err">文件必须是 .docx 格式</span>
        </div>
        <div class="field">
          <label>回收件（PDF）</label>
          <FileDrop
            v-model="targetFile"
            accept=".pdf"
            label="选择 .pdf 文件"
            hint="待核验的盖章或扫描件"
          />
          <span v-if="targetFile && !targetValid" class="err">文件必须是 .pdf 格式</span>
        </div>
      </div>

      <CompareApiPipelineTest
        :source-file="sourceFile"
        :target-file="targetFile"
        :enabled="configStore.config?.external_enabled === true"
        :public-base-url="configStore.config?.external_public_base_url || ''"
      />
    </section>
  </div>
</template>

<style scoped>
.api-workbench {
  max-width: 960px;
  margin: 0 auto;
}
.workbench-header {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: 32px;
  padding: 10px 2px 28px;
}
.eyebrow {
  margin: 0 0 8px;
  color: var(--primary);
  font-family: var(--mono);
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 0.12em;
  text-transform: uppercase;
}
.workbench-header h1 {
  margin: 0;
  font-size: clamp(28px, 4vw, 42px);
  line-height: 1.12;
  letter-spacing: -0.035em;
}
.header-copy {
  max-width: 620px;
  margin: 10px 0 0;
  color: var(--text-muted);
  font-size: 15px;
}
.service-state {
  display: flex;
  flex: 0 0 auto;
  align-items: center;
  gap: 10px;
  min-width: 118px;
  padding: 10px 13px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--surface);
}
.state-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--risk-medium);
  box-shadow: 0 0 0 4px var(--risk-medium-bg);
}
.service-state.enabled .state-dot {
  background: var(--risk-clean);
  box-shadow: 0 0 0 4px var(--risk-low-bg);
}
.service-state small,
.service-state strong {
  display: block;
  line-height: 1.35;
}
.service-state small {
  color: var(--text-muted);
  font-size: 11px;
}
.service-state strong {
  font-size: 13px;
}
.loading-state {
  margin: 0 0 12px;
  color: var(--text-muted);
}
.config-card,
.test-card {
  padding: 26px 28px 30px;
}
.section-heading {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 24px;
  margin-bottom: 24px;
}
.section-index {
  display: block;
  margin-bottom: 4px;
  color: var(--primary);
  font-family: var(--mono);
  font-size: 11px;
  font-weight: 700;
}
.section-heading h2 {
  margin: 0;
  font-size: 20px;
  letter-spacing: -0.015em;
}
.section-heading p {
  margin: 5px 0 0;
  color: var(--text-muted);
}
.section-heading > code {
  max-width: 46%;
  padding: 7px 9px;
  overflow: hidden;
  border: 1px solid var(--border);
  border-radius: 5px;
  color: var(--text-muted);
  background: var(--surface-2);
  font: 11px/1.4 var(--mono);
  text-overflow: ellipsis;
  white-space: nowrap;
}
.form-grid,
.file-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 18px;
}
.span-2 {
  grid-column: span 2;
}
.input-with-unit {
  position: relative;
}
.input-with-unit .input {
  padding-right: 52px;
  font-variant-numeric: tabular-nums;
}
.input-with-unit > span {
  position: absolute;
  top: 50%;
  right: 10px;
  color: var(--text-muted);
  font-size: 11px;
  transform: translateY(-50%);
}
.hint code {
  font-family: var(--mono);
}
.options-panel {
  display: grid;
  grid-template-columns: 0.8fr 1.3fr 1.2fr;
  gap: 22px;
  align-items: center;
  margin-top: 26px;
  padding: 20px;
  border-radius: var(--radius-sm);
  background: var(--surface-2);
}
.options-copy h3 {
  margin: 0;
  font-size: 15px;
}
.options-copy h3 span {
  margin-left: 4px;
  color: var(--text-muted);
  font-size: 11px;
  font-weight: 500;
}
.options-copy p {
  margin: 5px 0 0;
  color: var(--text-muted);
  font-size: 12px;
}
.engine-field {
  display: flex;
  gap: 8px;
  min-width: 0;
  margin: 0;
  padding: 0;
  border: 0;
}
.engine-field legend {
  position: absolute;
  width: 1px;
  height: 1px;
  overflow: hidden;
  clip: rect(0 0 0 0);
}
.choice {
  display: flex;
  flex: 1;
  align-items: center;
  gap: 8px;
  min-width: 0;
  padding: 9px 10px;
  border: 1px solid transparent;
  border-radius: 5px;
  background: var(--surface);
  cursor: pointer;
  transition: border-color 0.18s ease, transform 0.18s ease;
}
.choice:hover {
  transform: translateY(-1px);
}
.choice.selected {
  border-color: color-mix(in srgb, var(--primary) 55%, var(--border));
}
.choice span,
.choice strong,
.choice small,
.toggle-row span,
.toggle-row strong,
.toggle-row small {
  display: block;
}
.choice strong,
.toggle-row strong {
  font-size: 12px;
}
.choice small,
.toggle-row small {
  color: var(--text-muted);
  font-size: 10px;
}
.toggle-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 14px;
  cursor: pointer;
}
.option-toggles {
  display: grid;
  gap: 12px;
}
.toggle-row.disabled {
  cursor: not-allowed;
  opacity: 0.55;
}
.toggle-row input {
  width: 36px;
  height: 20px;
  accent-color: var(--primary);
}
.config-actions {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-top: 22px;
}
.save-success {
  color: var(--risk-clean);
  font-size: 13px;
  font-weight: 600;
}
.pipeline-test {
  margin-top: 20px;
}
.err {
  margin: 8px 0 0;
  color: var(--risk-high);
  font-size: 13px;
}
@media (max-width: 780px) {
  .workbench-header,
  .section-heading {
    align-items: flex-start;
    flex-direction: column;
  }
  .section-heading > code {
    max-width: 100%;
  }
  .options-panel {
    grid-template-columns: 1fr;
  }
}
@media (max-width: 620px) {
  .config-card,
  .test-card {
    padding: 20px;
  }
  .form-grid,
  .file-grid {
    grid-template-columns: 1fr;
  }
  .span-2 {
    grid-column: span 1;
  }
  .engine-field {
    flex-direction: column;
  }
  .config-actions {
    align-items: stretch;
    flex-direction: column;
  }
}
</style>
