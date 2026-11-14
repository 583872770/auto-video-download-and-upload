import { createRouter, createWebHistory } from 'vue-router';
import { confirmNavigation } from './composables/leaveGuard';
export const router = createRouter({
  history: createWebHistory(),
  routes: [
    {
      path: '/accounts',
      component: () => import('./features/accounts/AccountsPage.vue'),
      meta: { title: '账号管理' },
    },
    {
      path: '/',
      redirect: (to) => ({
        path:
          (
            {
              '#overview': '/workbench',
              '#tasks': '/logs',
              '#statistics': '/statistics',
              '#accounts': '/accounts',
              '#instances': '/instances',
              '#clash': '/proxy',
              '#deployment': '/deployment',
              '#settings': '/settings',
              '#media': '/media',
            } as Record<string, string>
          )[to.hash] || '/workbench',
        query: to.query,
      }),
    },
    {
      path: '/login',
      component: () => import('./features/auth/LoginPage.vue'),
      meta: { title: '管理账户', standalone: true },
    },
    {
      path: '/setup',
      component: () => import('./features/deployment/DeploymentPage.vue'),
      props: { setup: true },
      meta: { title: '本机部署与恢复', standalone: true },
    },
    {
      path: '/deployment',
      component: () => import('./features/deployment/DeploymentPage.vue'),
      meta: { title: '实例部署设置' },
    },
    {
      path: '/instances',
      component: () => import('./features/instances/InstancesPage.vue'),
      meta: { title: '下载实例' },
    },
    {
      path: '/statistics',
      component: () => import('./features/statistics/StatisticsPage.vue'),
      meta: { title: '交付与账单' },
    },
    {
      path: '/proxy',
      component: () => import('./features/proxy/ProxyPage.vue'),
      meta: { title: '网络与代理' },
    },
    {
      path: '/workbench',
      name: 'workbench',
      component: () => import('./features/workbench/WorkbenchPage.vue'),
      meta: { title: '下载工作台' },
    },
    {
      path: '/media',
      name: 'media',
      component: () => import('./features/media/MediaPage.vue'),
      meta: { title: '素材库' },
    },
    {
      path: '/logs',
      name: 'logs',
      component: () => import('./features/logs/LogsPage.vue'),
      meta: { title: '执行日志' },
    },
    {
      path: '/settings',
      name: 'settings',
      component: () => import('./features/settings/SettingsPage.vue'),
      meta: { title: '系统设置' },
    },
    {
      path: '/jobs',
      name: 'jobs',
      component: () => import('./features/jobs/JobsPage.vue'),
      meta: { title: '任务中心' },
    },
    {
      path: '/jobs/:id([a-f0-9]{32})',
      name: 'job',
      component: () => import('./features/jobs/JobDetail.vue'),
      meta: { title: '任务详情' },
    },
    {
      path: '/delivery-files',
      redirect: (to) => ({ path: '/media', query: to.query }),
    },
  ],
  scrollBehavior(to, from) {
    return to.path === from.path ? undefined : { top: 0 };
  },
});
router.beforeEach(async (to, from) => {
  if (to.path === '/jobs' && /^#[a-f0-9]{32}$/.test(to.hash))
    return { name: 'job', params: { id: to.hash.slice(1) }, query: to.query, replace: true };
  if (
    to.path === '/settings' &&
    from.path === '/settings' &&
    to.query.instance_id === from.query.instance_id
  )
    return true;
  return confirmNavigation();
});
router.afterEach((to) => {
  document.title = `${to.meta.title || '工作区'} · 帧流 Frameflow`;
});
