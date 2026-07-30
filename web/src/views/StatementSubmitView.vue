<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import FileDropMulti from '@/components/FileDropMulti.vue'
import StatementApiPipelineTest from '@/components/StatementApiPipelineTest.vue'
import { useModelConfigStore } from '@/stores/modelConfig'

const configStore = useModelConfigStore()
const targetFiles = ref<File[]>([])

const endpoint = computed(() => {
  const base = (configStore.config?.external_public_base_url || '').trim().replace(/\/$/, '')
  return `${base || 'https://compare.example.com'}/api/v1/external/amountStat`
})
const allPdf = computed(() =>
  targetFiles.value.length > 0 && targetFiles.value.every((file) => file.name.toLowerCase().endsWith('.pdf')),
)

onMounted(() => void configStore.fetch())
</script>

<template>
  <div class="api-workbench">
    <header class="workbench-header">
      <div>
        <p class="eyebrow">Amount statistics API</p>
        <h1>金额统计 API</h1>
        <p class="header-copy">使用真实 PDF 验证外部金额统计接口的 OCR、表格抽取、金额列定位和确定性求和。</p>
      </div>
      <div class="service-state" :class="{ enabled: configStore.config?.external_enabled }">
        <span class="state-dot" />
        <div><small>服务状态</small><strong>{{ configStore.config?.external_enabled ? '可调用' : '待配置' }}</strong></div>
      </div>
    </header>

    <p v-if="configStore.loading" class="loading-state">正在读取外部 API 配置…</p>

    <section class="card test-card">
      <div class="section-heading">
        <div>
          <span class="section-index">01</span>
          <h2>API 管线测试</h2>
          <p>上传对帐单 PDF，或在下方填写 PDF URL；两种方式可混合，测试任务不会发送回调。</p>
        </div>
        <code>POST {{ endpoint }}</code>
      </div>

      <p class="config-note">
        API Key、公开地址、上传限制与共用 OCR 默认引擎统一在
        <RouterLink to="/api-config">外部 API 配置</RouterLink>
        维护。
      </p>

      <div class="field">
        <label>对帐单 PDF（可多选）</label>
        <FileDropMulti v-model="targetFiles" accept=".pdf" label="选择一个或多个 .pdf 文件" hint="可与下方 PDF URL 混合，后端按顺序串行统计" />
        <span v-if="targetFiles.length > 0 && !allPdf" class="err">所有文件必须是 .pdf 格式</span>
      </div>
      <StatementApiPipelineTest
        :target-files="targetFiles"
        :enabled="configStore.config?.external_enabled === true"
        :public-base-url="configStore.config?.external_public_base_url || ''"
      />
    </section>
  </div>
</template>

<style scoped>
.api-workbench { max-width: 960px; margin: 0 auto; }
.workbench-header { display: flex; align-items: flex-end; justify-content: space-between; gap: 32px; padding: 10px 2px 28px; }
.eyebrow { margin: 0 0 8px; color: var(--primary); font-family: var(--mono); font-size: 11px; font-weight: 700; letter-spacing: .12em; text-transform: uppercase; }
.workbench-header h1 { margin: 0; font-size: clamp(28px, 4vw, 42px); line-height: 1.12; letter-spacing: -.035em; }
.header-copy { max-width: 620px; margin: 10px 0 0; color: var(--text-muted); font-size: 15px; }
.service-state { display: flex; flex: 0 0 auto; align-items: center; gap: 10px; min-width: 118px; padding: 10px 13px; border: 1px solid var(--border); border-radius: var(--radius-sm); background: var(--surface); }
.state-dot { width: 8px; height: 8px; border-radius: 50%; background: var(--risk-medium); box-shadow: 0 0 0 4px var(--risk-medium-bg); }.service-state.enabled .state-dot { background: var(--risk-clean); box-shadow: 0 0 0 4px var(--risk-low-bg); }.service-state small, .service-state strong { display: block; line-height: 1.35; }.service-state small { color: var(--text-muted); font-size: 11px; }.service-state strong { font-size: 13px; }
.loading-state { margin: 0 0 12px; color: var(--text-muted); }.test-card { padding: 26px 28px 30px; }.section-heading { display: flex; align-items: flex-start; justify-content: space-between; gap: 24px; margin-bottom: 18px; }.section-index { display: block; margin-bottom: 4px; color: var(--primary); font-family: var(--mono); font-size: 11px; font-weight: 700; }.section-heading h2 { margin: 0; font-size: 20px; }.section-heading p { margin: 5px 0 0; color: var(--text-muted); }.section-heading > code { max-width: 52%; min-width: 0; padding: 7px 9px; border: 1px solid var(--border); border-radius: 5px; color: var(--text-muted); background: var(--surface-2); font: 11px/1.4 var(--mono); overflow-wrap: anywhere; white-space: normal; }
.config-note { margin: 0 0 22px; padding: 10px 12px; border-left: 3px solid var(--primary); color: var(--text-muted); background: var(--surface-2); font-size: 13px; }.config-note a { color: var(--primary); font-weight: 600; }.err { margin: 8px 0 0; color: var(--risk-high); font-size: 13px; }
@media (max-width: 780px) { .workbench-header, .section-heading { align-items: flex-start; flex-direction: column; gap: 12px; }.section-heading > code { max-width: 100%; } } @media (max-width: 560px) { .test-card { padding: 20px; } }
</style>
