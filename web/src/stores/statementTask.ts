// 金额统计任务 store:与 stores/rawTask.ts 平行,持有 StatementSummaryReport。
// 提交、跟踪进度(SSE 主、轮询兜底)、终结后落 statement_report。
import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import {
  ApiError,
  getStatementTask,
  subscribeStatementProgress,
  submitStatement,
  type StatementSubmitArgs,
} from '@/api/statement'
import type { StatementSummaryReport, StatementTaskInfo, TaskStatus } from '@/api/types'

export type { StatementSubmitArgs }

const STATUS_ORDER: Record<TaskStatus, number> = {
  pending: 0,
  running: 1,
  done: 2,
  failed: 2,
}

export const useStatementTaskStore = defineStore('statementTask', () => {
  const taskId = ref<string | null>(null)
  const status = ref<TaskStatus>('pending')
  const stage = ref('')
  const progress = ref(0)
  const stageTimings = ref<Record<string, number>>({})
  const error = ref<string | null>(null)
  const report = ref<StatementSummaryReport | null>(null)
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
    stageTimings.value = {}
    error.value = null
    report.value = null
    elapsed.value = null
  }

  function applyInfo(info: StatementTaskInfo): void {
    if (STATUS_ORDER[info.status] < STATUS_ORDER[status.value] && !isTerminal.value) return
    taskId.value = info.task_id
    status.value = info.status
    stage.value = info.stage || stage.value
    progress.value = info.progress ?? progress.value
    stageTimings.value = info.stage_timings ?? stageTimings.value
    error.value = info.error
    elapsed.value = info.elapsed
    if (info.statement_report) report.value = info.statement_report
  }

  async function submit(args: StatementSubmitArgs): Promise<string> {
    reset()
    const resp = await submitStatement(args)
    taskId.value = resp.task_id
    status.value = resp.status ?? 'pending'
    beginStream(resp.task_id)
    return resp.task_id
  }

  /** 通过 id 接管任务:拉一次状态,未终结则开始跟随。 */
  async function load(id: string): Promise<void> {
    taskId.value = id
    const info = await getStatementTask(id)
    applyInfo(info)
    if (!isTerminal.value) beginStream(id)
  }

  function beginStream(id: string): void {
    abort?.abort()
    abort = subscribeStatementProgress(id, {
      onEvent: (ev) => {
        if (ev.stage) stage.value = ev.stage
        if (typeof ev.progress === 'number') progress.value = ev.progress
        if (ev.stage_timings) stageTimings.value = ev.stage_timings
        if (ev.status) status.value = ev.status
        if (ev.error) error.value = ev.error
      },
      onDone: async (finalStatus) => {
        status.value = finalStatus
        try {
          const info = await getStatementTask(id)
          applyInfo(info)
        } catch {
          // 终态已拿到,补拉失败不致命
        }
      },
      onError: (err) => {
        console.warn('[statementTask] SSE 失败,降级轮询:', err)
        fallbackPoll(id)
      },
    })
  }

  let pollTimer: ReturnType<typeof setInterval> | null = null
  async function fallbackPoll(id: string): Promise<void> {
    if (pollTimer) return
    const tick = async () => {
      try {
        const info = await getStatementTask(id)
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
    stageTimings,
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
