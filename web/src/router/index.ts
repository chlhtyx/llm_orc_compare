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
      path: '/settings',
      name: 'settings',
      component: () => import('@/views/ModelConfigView.vue'),
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
