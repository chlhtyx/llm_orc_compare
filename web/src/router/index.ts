import { createRouter, createWebHistory } from 'vue-router'

const router = createRouter({
  history: createWebHistory(import.meta.env.BASE_URL),
  routes: [
    {
      path: '/',
      name: 'submit',
      component: () => import('@/views/SubmitView.vue'),
    },
    {
      // 无标注版:纯文本 difflib 比对,独立流程(不经条款对齐/风险分级)。
      path: '/raw',
      name: 'raw-submit',
      component: () => import('@/views/RawSubmitView.vue'),
    },
    {
      path: '/raw/report/:taskId',
      name: 'raw-report',
      component: () => import('@/views/RawReportView.vue'),
      props: true,
    },
    {
      // 金额统计:多文件 PDF,串行 OCR + 代码确定性求和 + 聚合总金额。
      path: '/statement',
      name: 'statement-submit',
      component: () => import('@/views/StatementSubmitView.vue'),
    },
    {
      path: '/statement/report/:taskId',
      name: 'statement-report',
      component: () => import('@/views/StatementReportView.vue'),
      props: true,
    },
    {
      path: '/settings',
      name: 'settings',
      component: () => import('@/views/ModelConfigView.vue'),
    },
    {
      path: '/api-config',
      name: 'external-api-config',
      component: () => import('@/views/ExternalApiConfigView.vue'),
    },
    {
      // 比对记录:从 Postgres 查询历史任务列表与里程碑时间线。
      path: '/history',
      name: 'history',
      component: () => import('@/views/HistoryView.vue'),
    },
    {
      path: '/report/:taskId',
      name: 'report',
      component: () => import('@/views/ReportView.vue'),
      props: true,
    },
  ],
})

export default router
