// Report store:对 TamperReport 做派生统计(按风险分级、要素变更)。
import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import type { Diff, KeyElement, OverallRisk, TamperReport } from '@/api/types'

const RISK_RANK: Record<Diff['risk_level'], number> = {
  high: 0,
  medium: 1,
  low: 2,
  none: 3,
}

export const useReportStore = defineStore('report', () => {
  const report = ref<TamperReport | null>(null)

  function set(r: TamperReport | null): void {
    report.value = r
  }

  const diffs = computed<Diff[]>(() => report.value?.diffs ?? [])
  const keyElements = computed<KeyElement[]>(() => report.value?.key_elements ?? [])
  const unmatched = computed<Diff[]>(() => report.value?.unmatched_clauses ?? [])

  /** 按风险等级排序的条款差异(high → none)。 */
  const diffsBySeverity = computed<Diff[]>(() =>
    [...diffs.value].sort((a, b) => RISK_RANK[a.risk_level] - RISK_RANK[b.risk_level]),
  )

  const highRiskDiffs = computed<Diff[]>(() =>
    diffs.value.filter((d) => d.risk_level === 'high'),
  )

  const changedKeyElements = computed<KeyElement[]>(() =>
    keyElements.value.filter((k) => k.changed),
  )

  const counts = computed(() => {
    const total = diffs.value.length
    const modified = diffs.value.filter((d) => d.status === 'modified').length
    const added = diffs.value.filter((d) => d.status === 'added').length
    const deleted = diffs.value.filter((d) => d.status === 'deleted').length
    const identical = diffs.value.filter((d) => d.status === 'identical').length
    return { total, modified, added, deleted, identical, unmatched: unmatched.value.length }
  })

  const overallRisk = computed<OverallRisk>(() => report.value?.overall_risk ?? 'clean')

  return {
    report,
    diffs,
    keyElements,
    unmatched,
    diffsBySeverity,
    highRiskDiffs,
    changedKeyElements,
    counts,
    overallRisk,
    set,
  }
})
