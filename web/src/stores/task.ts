// 任务 store:提交、跟踪进度(SSE 主、轮询兜底)、终结后落 report 引用。
import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import {
  ApiError,
  getTask,
  submitCompare,
  subscribeProgress,
  type SubmitArgs,
} from '@/api/compare'
import type { TaskInfo, TaskStatus, TamperReport } from '@/api/types'

export type { SubmitArgs }

const STATUS_ORDER: Record<TaskStatus, number> = {
  pending: 0,
  running: 1,
  done: 2,
  failed: 2,
}

export const useTaskStore = defineStore('task', () => {
  const taskId = ref<string | null>(null)
  const status = ref<TaskStatus>('pending')
  const stage = ref('')
  const progress = ref(0)
 const overallRisk = ref<TaskInfo['overall_risk']>(null)
 const error = ref<string | null>(null)
 const report = ref<TamperReport | null>(null)
  const elapsed = ref<number | null>(null)

  const isTerminal = computed(() => status.value === 'done' || status.value === 'failed')
  const isRunning = computed(() => status.value === 'pending' || status.value === 'running')

  let abort: AbortController | null = null

  function reset(): void {
    abort?.abort()
    abort = null
    taskId.value = null
    status.value = 'pending'
    stage.value = ''
    progress.value = 0
    overallRisk.value = null
   error.value = null
   report.value = null
    elapsed.value = null
 }

  function applyInfo(info: TaskInfo): void {
    // 后端进度可能乱序回跳,只接受单调推进
    if (STATUS_ORDER[info.status] < STATUS_ORDER[status.value] && !isTerminal.value) return
    taskId.value = info.task_id
    status.value = info.status
    stage.value = info.stage || stage.value
   progress.value = info.progress ?? progress.value
   overallRisk.value = info.overall_risk ?? overallRisk.value
   error.value = info.error
    elapsed.value = info.elapsed
   if (info.report) report.value = info.report
  }

  async function submit(args: SubmitArgs): Promise<string> {
    reset()
    const resp = await submitCompare(args)
    taskId.value = resp.task_id
    status.value = resp.status ?? 'pending'
    beginStream(resp.task_id)
    return resp.task_id
  }

  /** 通过 id 接管一个任务(如直接打开报告链接):拉一次状态,未终结则开始跟随。 */
  async function load(id: string): Promise<void> {
    taskId.value = id
    const info = await getTask(id)
    applyInfo(info)
    if (!isTerminal.value) beginStream(id)
  }

  /** 订阅 SSE;流结束后再拉一次 getTask 以确保拿到最终 report。 */
  function beginStream(id: string): void {
    abort?.abort()
    abort = subscribeProgress(id, {
      onEvent: (ev) => {
        if (ev.stage) stage.value = ev.stage
        if (typeof ev.progress === 'number') progress.value = ev.progress
        if (ev.status) status.value = ev.status
        if ('overall_risk' in ev && ev.overall_risk) overallRisk.value = ev.overall_risk
        if (ev.error) error.value = ev.error
      },
      onDone: async (finalStatus) => {
        status.value = finalStatus
        // 流结束不代表数据已就位,主动补拉一次
        try {
          const info = await getTask(id)
          applyInfo(info)
        } catch {
          // 终态已拿到,补拉失败不致命
        }
      },
      onError: (err) => {
        // SSE 不可用(代理/鉴权问题时)→ 降级轮询
        console.warn('[task] SSE 失败,降级轮询:', err)
        fallbackPoll(id)
      },
    })
  }

  let pollTimer: ReturnType<typeof setInterval> | null = null
  async function fallbackPoll(id: string): Promise<void> {
    if (pollTimer) return
    const tick = async () => {
      try {
        const info = await getTask(id)
        applyInfo(info)
        if (info.status === 'done' || info.status === 'failed') {
          stopPoll()
        }
      } catch (e) {
        error.value = e instanceof ApiError ? e.message : `轮询失败: ${(e as Error).message}`
        stopPoll()
      }
    }
    await tick()
    pollTimer = setInterval(tick, 1500)
  }

  function stopPoll(): void {
    if (pollTimer) {
      clearInterval(pollTimer)
      pollTimer = null
    }
  }

  function dispose(): void {
    abort?.abort()
    abort = null
    stopPoll()
  }

  return {
    taskId,
    status,
    stage,
    progress,
    overallRisk,
   error,
   report,
    elapsed,
   isTerminal,
    isRunning,
    reset,
    submit,
    load,
    applyInfo,
    dispose,
  }
})
