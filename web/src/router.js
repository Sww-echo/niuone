import { createRouter, createWebHistory } from 'vue-router'

const dashboardPaths = [
  '/',
  '/candidates',
  '/practice',
  '/watchlist',
  '/technical-analysis',
  '/niuone-mainline',
  '/indices',
  '/industry-flow',
  '/dragon-tiger',
  '/market-monitor',
  '/realtime-news',
]

const routes = [
  ...dashboardPaths.map(path => ({
    path,
    component: () => import('./components/DashboardPage.vue'),
    meta: { dashboardHeader: true },
  })),
  {
    path: '/admin',
    component: () => import('./components/AdminPage.vue'),
    meta: { dashboardHeader: true },
  },
  {
    path: '/admin/backtest/:strategyId',
    component: () => import('./components/AdminBacktestPage.vue'),
  },
  {
    path: '/admin/settings/:group',
    component: () => import('./components/AdminPage.vue'),
    meta: { dashboardHeader: true },
  },
  {
    path: '/:pathMatch(.*)*',
    component: () => import('./components/NotFoundPage.vue'),
  },
]

export default createRouter({
  history: createWebHistory(),
  routes,
})
