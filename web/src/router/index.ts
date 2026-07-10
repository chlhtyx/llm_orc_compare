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
