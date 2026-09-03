<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import * as pdfjsLib from 'pdfjs-dist'
import type { Diff, PageMeta } from '@/api/types'

// 设置 pdf.js worker
pdfjsLib.GlobalWorkerOptions.workerSrc = new URL(
  'pdfjs-dist/build/pdf.worker.min.mjs',
  import.meta.url,
).toString()

const props = defineProps<{
  pdfUrl?: string | null
  pageMeta: PageMeta[]
  diffs: Diff[]
  side?: 'source' | 'target'
  /** 双栏预览时按容器宽度缩小页面，避免窄屏退化为单栏或横向溢出。 */
  fitToContainer?: boolean
  /** 双栏预览共用的缩放倍率；1 表示适宽，放大后可在页面内横向滚动查看。 */
  zoomLevel?: number
  selectedClauseId?: string | null
}>()

const emit = defineEmits<{
  selectClause: [clauseId: string]
  'update:zoomLevel': [zoomLevel: number]
}>()

// ---- 状态 ----
const loading = ref(true)
const loadError = ref<string | null>(null)
const pages = ref<{
  pageNum: number
  canvas: HTMLCanvasElement
  width: number
  height: number
  meta: PageMeta | null
}[]>([])
const viewerRoot = ref<HTMLElement | null>(null)
let resizeObserver: ResizeObserver | null = null
let renderedContainerWidth = 0

// ---- 整理高亮区域 ----
interface HighlightRegion {
  diffId: string
  label: string
  risk: string
  pageIndex: number
  bbox: number[]   // 归一化 [x1,y1,x2,y2]
  kind: 'real' | 'placeholder'  // placeholder=推断占位框(deleted),位置非精确
}

const allRegions = computed<HighlightRegion[]>(() =>
  props.diffs
    .filter((d) => (props.side === 'source' ? (d.source_page_regions ?? []) : d.page_regions).length)
    .flatMap((d) =>
      (props.side === 'source' ? (d.source_page_regions ?? []) : d.page_regions).map((r) => ({
        diffId: d.alignment_id,
        label: [d.number, d.title].filter(Boolean).join(' · ') || d.alignment_id,
        risk: d.risk_level,
        pageIndex: r.page_index,
        bbox: r.bbox,
        kind: r.kind ?? 'real',
      })),
    ),
)

/** 按页分组的高亮区域 */
const regionsByPage = computed<Record<number, HighlightRegion[]>>(() => {
  const map: Record<number, HighlightRegion[]> = {}
  for (const r of allRegions.value) {
    const idx = r.pageIndex
    if (!map[idx]) map[idx] = []
    map[idx].push(r)
  }
  return map
})

// ---- 风险色映射 ----
function riskColor(level: string): string {
  switch (level) {
    case 'high': return 'rgba(220,38,38,0.35)'
    case 'medium': return 'rgba(234,179,8,0.35)'
    case 'low': return 'rgba(59,130,246,0.30)'
    default: return 'rgba(148,163,184,0.25)'
  }
}

// ---- deleted 推断占位框:虚线红框 + 极淡填充(与真实高亮区分)----
const placeholderFill = 'rgba(220,38,38,0.10)'
const placeholderBorder = 'rgba(220,38,38,0.85)'

function riskBorder(level: string): string {
  switch (level) {
    case 'high': return 'rgba(220,38,38,0.80)'
    case 'medium': return 'rgba(234,179,8,0.70)'
    case 'low': return 'rgba(59,130,246,0.60)'
    default: return 'rgba(148,163,184,0.40)'
  }
}

// ---- 归一化 bbox → CSS 显示坐标 ----
function rectFromBbox(bbox: number[], pageW: number, pageH: number) {
  const [x1, y1, x2, y2] = bbox
  const left = x1 * pageW
  const top = y1 * pageH
  const width = (x2 - x1) * pageW
  const height = (y2 - y1) * pageH
  return { left: `${left}px`, top: `${top}px`, width: `${width}px`, height: `${height}px` }
}

// ---- hover/tooltip ----
const hoveredRegion = ref<HighlightRegion | null>(null)
const tooltipStyle = ref<Record<string, string>>({})

function showTooltip(event: MouseEvent, region: HighlightRegion, pageIdx: number) {
  const pg = pages.value[pageIdx]
  if (!pg) return
  const rect = (event.currentTarget as HTMLElement).getBoundingClientRect()
  const bbox = region.bbox
  hoveredRegion.value = region
  tooltipStyle.value = {
    left: `${bbox[0] * rect.width}px`,
    top: `${bbox[1] * rect.height - 28}px`,
  }
}
function hideTooltip() {
  hoveredRegion.value = null
}

// ---- 选中高亮 ----
function regionIsSelected(r: HighlightRegion): boolean {
  return props.selectedClauseId === r.diffId
}

// ---- 放大 ----
const localZoom = ref(1.0)
const zoom = computed(() => props.zoomLevel ?? localZoom.value)

function setZoom(nextZoom: number): void {
  const boundedZoom = Math.max(0.5, Math.min(4, nextZoom))
  if (props.zoomLevel == null) {
    localZoom.value = boundedZoom
    void renderPdf()
    return
  }
  emit('update:zoomLevel', boundedZoom)
}

// ---- 滚动到指定页 ----
const pageRefs = ref<Map<number, HTMLElement>>(new Map())

function setPageRef(pageNum: number, el: Element | null) {
  if (el) pageRefs.value.set(pageNum, el as HTMLElement)
  else pageRefs.value.delete(pageNum)
}

function scrollToPage(pageNum: number) {
  const el = pageRefs.value.get(pageNum)
  el?.scrollIntoView({ behavior: 'smooth', block: 'start' })
}

/**
 * 选中条款时定位到本侧首个真实或推断区域。
 *
 * 列表点击会先让父组件展开预览，再挂载 PdfViewer；因此不能只依赖 prop
 * 改变时的 watcher，还要在 PDF 页面实际渲染、页面 ref 已挂载后再执行一次。
 */
function scrollToSelectedClause() {
  const id = props.selectedClauseId
    if (!id) return
  const region = allRegions.value.find((r) => r.diffId === id)
  if (region) scrollToPage(region.pageIndex)
}

watch(
  () => props.zoomLevel,
  (nextZoom, previousZoom) => {
    if (nextZoom != null && nextZoom !== previousZoom) void renderPdf()
  },
)

watch(
  () => props.selectedClauseId,
  () => {
    void nextTick(scrollToSelectedClause)
  },
  { flush: 'post' },
)


// ---- 设备像素比 ----
const dpr = ref(window.devicePixelRatio || 2)

// ---- 渲染 PDF ----
async function renderPdf() {
  if (!props.pdfUrl) return
  loading.value = true
  loadError.value = null
  pages.value = []

  try {
    const pdf = await pdfjsLib.getDocument({ url: props.pdfUrl, cMapUrl: undefined, cMapPacked: true }).promise
    const newPages: typeof pages.value = []
    const availableWidth = viewerRoot.value?.clientWidth ?? 0
    renderedContainerWidth = availableWidth

    for (let i = 1; i <= pdf.numPages; i++) {
      const page = await pdf.getPage(i)
      const meta = props.pageMeta?.find((m) => m.page_index === i - 1) ?? null

      // 使用设备像素比保证在各平台清晰且高亮对齐
      const baseViewport = page.getViewport({ scale: 1 })
      const fit = props.fitToContainer && availableWidth > 0
        ? Math.min(1, Math.max(0.2, (availableWidth - 8) / baseViewport.width))
        : 1
      const scale = dpr.value * zoom.value * fit
      const viewport = page.getViewport({ scale })
      const canvas = document.createElement('canvas')
      canvas.width = viewport.width
      canvas.height = viewport.height

      const ctx = canvas.getContext('2d')!
      await page.render({ canvasContext: ctx, viewport }).promise

      newPages.push({
        pageNum: i,
        canvas,
        width: viewport.width / dpr.value,   // CSS 显示尺寸
        height: viewport.height / dpr.value,
        meta,
      })
    }

    pages.value = newPages
    // 首次由条款列表展开预览时 selectedClauseId 已存在，等 ref 完整挂载后再跳转。
    await nextTick()
    scrollToSelectedClause()
  } catch (e) {
    loadError.value = `PDF 加载失败: ${(e as Error).message}`
  } finally {
    loading.value = false
  }
}

/** 把 canvas 挂载到 DOM 后，替换 canvas 元素 */
function mountCanvas(canvas: HTMLCanvasElement, container: HTMLElement) {
  container.innerHTML = ''
  container.appendChild(canvas)
}

watch(() => props.pdfUrl, () => { void renderPdf() })
onMounted(() => {
  if (viewerRoot.value && props.fitToContainer) {
    resizeObserver = new ResizeObserver(([entry]) => {
      const width = entry?.contentRect.width ?? 0
      if (Math.abs(width - renderedContainerWidth) > 8) void renderPdf()
    })
    resizeObserver.observe(viewerRoot.value)
  }
  void renderPdf()
})
onBeforeUnmount(() => {
  resizeObserver?.disconnect()
  pages.value = []
})
</script>

<template>
  <div ref="viewerRoot" class="pv">
    <div v-if="loading" class="pv-loading muted">PDF 加载中…</div>
    <p v-else-if="loadError" class="pv-err">{{ loadError }}</p>

    <!-- 缩放控件 -->
    <div v-if="pages.length" class="pv-toolbar">
      <button class="chip" @click="setZoom(zoom - 0.25)">-</button>
      <span class="zoom-label">{{ Math.round(zoom * 100) }}%</span>
      <button class="chip" @click="setZoom(zoom + 0.25)">+</button>
      <button class="chip pv-fit-button" :disabled="zoom === 1" @click="setZoom(1)">适宽</button>
    </div>

    <div v-if="pages.length" class="pv-pages-viewport">
      <div class="pv-pages">
        <div
          v-for="(p, pi) in pages"
          :key="p.pageNum"
          :ref="(el) => setPageRef(pi, el as Element)"
          class="pv-page"
          :style="{
            width: `${p.width}px`,
            aspectRatio: `${p.width} / ${p.height}`,
          }"
        >
        <!-- canvas 层 -->
        <div class="pv-canvas-wrap" :ref="(el) => el && mountCanvas(p.canvas, el as HTMLElement)" />

        <!-- 高亮叠加层 -->
        <div v-if="regionsByPage[pi]" class="pv-overlay">
          <div
            v-for="r in regionsByPage[pi]"
            :key="r.diffId + r.bbox.join(',')"
            class="pv-highlight"
            :class="{
              'pv-highlight--placeholder': r.kind === 'placeholder',
              'pv-highlight--selected': regionIsSelected(r),
              'pv-highlight--hover': hoveredRegion === r,
            }"
            :style="{
              ...rectFromBbox(r.bbox, p.width, p.height),
              backgroundColor: r.kind === 'placeholder' ? placeholderFill : riskColor(r.risk),
              borderColor: r.kind === 'placeholder' ? placeholderBorder : riskBorder(r.risk),
            }"
            @click="emit('selectClause', r.diffId)"
            @mouseenter="showTooltip($event, r, pi)"
            @mousemove="showTooltip($event, r, pi)"
            @mouseleave="hideTooltip"
          />
        </div>

        <!-- 页码 -->
          <span class="pv-page-num">{{ p.pageNum }}</span>
        </div>
      </div>
    </div>

    <!-- 条款列表 -->
    <section v-if="allRegions.length" class="pv-regions">
      <h4 class="section-title">高亮区域({{ allRegions.length }})</h4>
      <ul class="region-list">
        <li
          v-for="r in allRegions"
          :key="r.diffId + r.bbox.join(',')"
          class="region-item"
          :class="{ 'region-item--sel': regionIsSelected(r) }"
          @click="emit('selectClause', r.diffId); scrollToPage(r.pageIndex)"
        >
          <span class="badge" :class="`badge-${r.risk}`">{{ r.risk }}</span>
          <span class="r-label">{{ r.label }}</span>
          <span class="muted">第 {{ r.pageIndex + 1 }} 页</span>
        </li>
      </ul>
    </section>
  </div>
</template>

<style scoped>
.pv {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.pv-loading,
.pv-err {
  padding: 24px;
  text-align: center;
}
.pv-err {
  color: var(--risk-high);
}
.pv-toolbar {
  display: flex;
  align-items: center;
  gap: 8px;
}
.zoom-label {
  font-size: 12px;
  min-width: 36px;
  text-align: center;
  color: var(--text-muted);
}
.pv-fit-button {
  margin-left: 4px;
}
.pv-pages-viewport {
  width: 100%;
  overflow-x: auto;
  overscroll-behavior-x: contain;
  scrollbar-gutter: stable;
}
.pv-pages {
  width: max-content;
  min-width: 100%;
  display: flex;
  flex-direction: column;
  gap: 16px;
  align-items: flex-start;
}
.pv-page {
  flex: 0 0 auto;
  margin-inline: auto;
  position: relative;
  border: 1px solid var(--border);
  border-radius: 2px;
  box-shadow: 0 1px 4px rgba(0,0,0,.12);
  overflow: hidden;
  background: #fff;
}
.pv-canvas-wrap {
  position: absolute;
  inset: 0;
}
.pv-canvas-wrap :deep(canvas) {
  display: block;
  width: 100%;
  height: auto;
}
.pv-overlay {
  position: absolute;
  inset: 0;
  pointer-events: auto;
}
.pv-highlight {
  position: absolute;
  border-width: 2px;
  border-style: solid;
  border-radius: 2px;
  cursor: pointer;
  transition: box-shadow 0.12s, background-color 0.12s;
  box-sizing: border-box;
}
.pv-highlight--placeholder {
  border-style: dashed;
}
.pv-highlight:hover,
.pv-highlight--hover {
  box-shadow: 0 0 0 3px rgba(59,130,246,.45);
  z-index: 2;
}
.pv-highlight--selected {
  box-shadow: 0 0 0 3px rgba(220,38,38,.6);
  z-index: 3;
}
.pv-page-num {
  position: absolute;
  bottom: 4px;
  right: 8px;
  font-size: 11px;
  color: #94a3b8;
  user-select: none;
  pointer-events: none;
}
.pv-regions {
  /* nothing extra */
}
.region-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 6px;
  font-size: 13px;
}
.region-item {
  display: flex;
  align-items: center;
  gap: 8px;
  cursor: pointer;
  padding: 4px 6px;
  border-radius: 4px;
  transition: background 0.1s;
}
.region-item:hover {
  background: var(--surface-2);
}
.region-item--sel {
  background: rgba(59,130,246,.1);
  outline: 1px solid rgba(59,130,246,.3);
}
.r-label {
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
</style>
