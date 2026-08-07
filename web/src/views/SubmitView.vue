<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue'
import FileDrop from '@/components/FileDrop.vue'
import CompareApiPipelineTest from '@/components/CompareApiPipelineTest.vue'
import type { LlmConfig } from '@/api/config'
import { useModelConfigStore } from '@/stores/modelConfig'

const configStore = useModelConfigStore()

const sourceFile = ref<File | null>(null)
const targetFile = ref<File | null>(null)
const saved = ref(false)
const saveError = ref<string | null>(null)

const form = reactive({
  external_enable_llm_judge: false,
  external_enable_llm_alignment: false,
  external_enable_risk_assessment: false,
  external_enable_llm_direct_diff: false,
  external_truncate_to_original_pages: false,
  // —— LLM 直接比对系统提示词(留空=内置默认)——
  llm_direct_diff_prompt: '',
  // —— /no_think 指令开关(GLM-4.5/4.6 关闭思考链;默认 True=保持当前行为)——
  llm_diff_no_think_enabled: true,
})

const sourceValid = computed(() => {
  const name = sourceFile.value?.name.toLowerCase() ?? ''
  return name.endsWith('.docx') || name.endsWith('.pdf')
})
const targetValid = computed(() => !!targetFile.value?.name.toLowerCase().endsWith('.pdf'))
// LLM 直接比对与标准管线(对齐/辅助说明)互斥:开启直接比对时这两个选项被忽略,
// UI 联动禁用 + 保存时强制写 false,与「辅助说明依赖风险评估」的处理保持一致。
const directDiffBlocksStandard = computed(() => form.external_enable_llm_direct_diff)
// 内置默认提示词只读展示开关(折叠面板)
const showDefaultPrompt = ref(false)
const endpoint = computed(() => {
  const base = (configStore.config?.external_public_base_url || '').trim().replace(/\/$/, '')
  return `${base || 'https://compare.example.com'}/api/v1/external/contractCompare`
})

function syncFromConfig(config: LlmConfig | null): void {
  if (!config) return
  form.external_enable_llm_judge = config.external_enable_llm_judge ?? false
  form.external_enable_llm_alignment = config.external_enable_llm_alignment ?? false
  form.external_enable_risk_assessment = config.external_enable_risk_assessment ?? false
  form.external_enable_llm_direct_diff = config.external_enable_llm_direct_diff ?? false
  form.external_truncate_to_original_pages = config.external_truncate_to_original_pages ?? false
  form.llm_direct_diff_prompt = config.llm_direct_diff_prompt || ''
  form.llm_diff_no_think_enabled = config.llm_diff_no_think_enabled ?? true
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
  // LLM 直接比对分支会早返回,标准管线的对齐/辅助说明不执行:
  // 开启直接比对时强制把这两个选项写 false,避免「以为在跑」的误解。
  const directDiffOn = form.external_enable_llm_direct_diff
  const ok = await configStore.save({
    external_enable_llm_judge:
      !directDiffOn &&
      form.external_enable_risk_assessment &&
      form.external_enable_llm_judge,
    external_enable_llm_alignment: !directDiffOn && form.external_enable_llm_alignment,
    external_enable_risk_assessment: form.external_enable_risk_assessment,
    external_enable_llm_direct_diff: form.external_enable_llm_direct_diff,
    external_truncate_to_original_pages: form.external_truncate_to_original_pages,
    // 每次都提交:空串=回退内置默认(用户清空文本框即清除自定义)。
    llm_direct_diff_prompt: form.llm_direct_diff_prompt.trim(),
    llm_diff_no_think_enabled: form.llm_diff_no_think_enabled,
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
          用真实合同验证从识别、比对到高亮图片的完整外部 API 管线。
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
          <h2>比对默认选项</h2>
          <p>仅维护合同比对的流程选项；通用访问配置统一在外部 API 配置页维护。</p>
        </div>
        <code>POST {{ endpoint }}</code>
      </div>

      <div class="options-panel">
        <div class="options-copy">
          <h3>比对选项 <span>可选</span></h3>
          <p>作为 API 的默认比对配置，正式调用与测试任务保持一致。</p>
          <RouterLink to="/api-config" class="config-link">维护通用 API 配置</RouterLink>
        </div>

       <div class="option-toggles">
          <label class="toggle-row">
            <span>
              <strong>LLM 直接比对</strong>
              <small>开启后跳过条款切分/对齐，解析后直接把 Word 与 PDF 文本交给 LLM 比对差异并标注；不做风险分级、不生成 PDF 高亮框。</small>
            </span>
            <input
              v-model="form.external_enable_llm_direct_diff"
              type="checkbox"
              role="switch"
            />
          </label>
         <label class="toggle-row" :class="{ disabled: directDiffBlocksStandard }">
           <span>
             <strong>LLM 联合分段对齐</strong>
              <small>与 LLM 直接比对互斥；直接对齐 DOCX 段落与 PDF OCR 块，失败时回退规则，不会决定是否发生篡改。</small>
            </span>
            <input
              v-model="form.external_enable_llm_alignment"
              type="checkbox"
              role="switch"
              :disabled="directDiffBlocksStandard"
            />
          </label>
          <label class="toggle-row">
            <span>
              <strong>风险评估</strong>
              <small>开启高、中、低风险分级与高风险要素抽取。</small>
            </span>
            <input v-model="form.external_enable_risk_assessment" type="checkbox" role="switch" />
          </label>
          <label
            class="toggle-row"
            :class="{ disabled: !form.external_enable_risk_assessment || directDiffBlocksStandard }"
          >
            <span>
              <strong>LLM 辅助说明</strong>
              <small>依赖风险评估，与 LLM 直接比对互斥；只补充解释，不会撤销确定变化。</small>
            </span>
            <input
              v-model="form.external_enable_llm_judge"
              type="checkbox"
              role="switch"
              :disabled="!form.external_enable_risk_assessment || directDiffBlocksStandard"
            />
          </label>
          <label class="toggle-row">
            <span>
              <strong>回收件页数截取</strong>
              <small>
                自动按 Word 保存页数截掉合同后的图纸；请求可显式提供真实页数覆盖。
              </small>
            </span>
            <input
              v-model="form.external_truncate_to_original_pages"
              type="checkbox"
              role="switch"
            />
          </label>
          <label class="toggle-row">
            <span>
              <strong>关闭 GLM 思考链 (/no_think)</strong>
              <small>
                比对调用系统提示词末尾追加 /no_think，关闭 GLM-4.5/4.6 的 &lt;think&gt; 思考链 token
                （与 Qwen3 的 enable_thinking=False 并存）。影响 LLM 直接比对与风险复核两条通道；
                非 GLM 模型自动忽略。
              </small>
            </span>
            <input
              v-model="form.llm_diff_no_think_enabled"
              type="checkbox"
              role="switch"
            />
          </label>
        </div>
      </div>

      <div v-if="form.external_enable_llm_direct_diff" class="prompt-panel">
        <div class="prompt-copy">
          <h3>LLM 直接比对提示词 <span>可选</span></h3>
          <p>
            自定义交给 LLM 的系统提示词(复用设置页 judge_* 纯文本模型)。<b>留空</b>使用内置默认规则;
            自定义时<b>必须保留</b>「严格输出 JSON」「hunks / similarity 结构」等输出契约,否则解析失败会报错。
          </p>
        </div>
        <textarea
          v-model="form.llm_direct_diff_prompt"
          class="input prompt-textarea"
          rows="12"
          spellcheck="false"
          placeholder="留空使用内置默认规则。内置规则要求 LLM 忽略 OCR 排版噪声,只报出金额/日期/主体/账号等关键要素的实质性改动,并严格输出 {hunks:[...], similarity:0~1} 的 JSON。"
        ></textarea>
        <button
          type="button"
          class="default-prompt-toggle"
          :aria-expanded="showDefaultPrompt"
          @click="showDefaultPrompt = !showDefaultPrompt"
        >
          {{ showDefaultPrompt ? '▼' : '▸' }} 查看内置默认规则
        </button>
        <pre v-if="showDefaultPrompt" class="default-prompt-view">{{ configStore.config?.llm_direct_diff_default_prompt }}</pre>
        <span class="hint">
          <template v-if="configStore.config?.llm_direct_diff_prompt">
            当前:自定义提示词({{ configStore.config.llm_direct_diff_prompt.length }} 字)
          </template>
          <template v-else>当前:内置默认规则</template>
          · 清空文本框并保存即回退内置默认
        </span>
      </div>

      <div class="config-actions">
        <button class="btn btn-primary" :disabled="configStore.saving" @click="onSave">
          {{ configStore.saving ? '保存中…' : '保存比对选项' }}
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
          <label>原始合同（Word / PDF）</label>
          <FileDrop
            v-model="sourceFile"
            accept=".docx,.pdf"
            label="选择 .docx 或 .pdf 文件"
            hint="作为比对基准的原始合同"
          />
          <span v-if="sourceFile && !sourceValid" class="err">文件必须是 .docx 或 .pdf 格式</span>
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
.hint .link-btn {
  margin-left: 6px;
  padding: 0;
  border: 0;
  background: none;
  color: var(--primary);
  font: inherit;
  font-size: 12px;
  text-decoration: underline;
  text-underline-offset: 2px;
  cursor: pointer;
}
.hint .link-btn.danger {
  color: var(--risk-high);
}
.hint .pending-clear {
  margin-left: 6px;
  color: var(--risk-high);
  font-size: 12px;
}
.options-panel {
  display: grid;
  grid-template-columns: 0.8fr 1.6fr;
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
.prompt-panel {
  margin-top: 18px;
  padding: 20px;
  border-radius: var(--radius-sm);
  background: var(--surface-2);
}
.prompt-copy h3 {
  margin: 0;
  font-size: 15px;
}
.prompt-copy h3 span {
  margin-left: 4px;
  color: var(--text-muted);
  font-size: 11px;
  font-weight: 500;
}
.prompt-copy p {
  margin: 5px 0 12px;
  color: var(--text-muted);
  font-size: 12px;
}
.prompt-textarea {
  width: 100%;
  font-family: var(--mono);
  font-size: 13px;
  line-height: 1.5;
  resize: vertical;
  min-height: 180px;
}
.default-prompt-toggle {
  align-self: flex-start;
  margin-top: 10px;
  padding: 0;
  border: 0;
  background: none;
  color: var(--primary);
  font: inherit;
  font-size: 12px;
  font-weight: 600;
  cursor: pointer;
}
.default-prompt-toggle:hover {
  text-decoration: underline;
  text-underline-offset: 2px;
}
.default-prompt-view {
  max-height: 340px;
  margin: 8px 0 0;
  padding: 12px 14px;
  border: 1px solid var(--border);
  border-radius: 5px;
  background: var(--surface);
  color: var(--text-muted);
  font: 12px/1.55 var(--mono);
  white-space: pre-wrap;
  overflow: auto;
}
.config-link {
  display: inline-block;
  margin-top: 10px;
  color: var(--primary);
  font-size: 12px;
  font-weight: 600;
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
