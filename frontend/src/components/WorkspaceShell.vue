<script setup lang="ts">
import { onMounted, onUnmounted, ref } from 'vue';
import { RouterLink, useRoute } from 'vue-router';
import { bootstrap } from '../bootstrap';
import AppIcon from './AppIcon.vue';
import { api } from '../api';
import { confirmNavigation, hasPendingEdits } from '../composables/leaveGuard';
const route = useRoute(),
  menuOpen = ref(false),
  error = ref(''),
  loggingOut = ref(false);
const groups = [
  {
    label: '日常工作',
    links: [
      { url: '/jobs', label: '任务中心', icon: 'grid' },
      { url: '/workbench', label: '下载工作台', icon: 'download' },
      { url: '/media', label: '素材库', icon: 'media' },
      { url: '/statistics', label: '交付与账单', icon: 'chart' },
      { url: '/accounts', label: '账号管理', icon: 'users' },
    ],
  },
  {
    label: '运行与排查',
    links: [
      ...(bootstrap().fleetEnabled
        ? [{ url: '/instances', label: '下载实例', icon: 'server' }]
        : []),
      { url: '/logs', label: '执行日志', icon: 'logs' },
    ],
  },
  {
    label: '配置与维护',
    links: [
      { url: '/proxy', label: '网络与代理', icon: 'network' },
      { url: '/settings', label: '系统设置', icon: 'settings' },
      { url: '/deployment', label: '部署设置', icon: 'deploy' },
    ],
  },
];
function beforeUnload(event: BeforeUnloadEvent) {
  if (!loggingOut.value && hasPendingEdits()) {
    event.preventDefault();
    event.returnValue = '';
  }
}
async function logout() {
  if (loggingOut.value || !(await confirmNavigation())) return;
  try {
    await api.request('/api/auth/logout', { method: 'POST', body: {} });
    loggingOut.value = true;
    location.assign('/login');
  } catch (failure) {
    error.value = String(failure);
  }
}
onMounted(() => window.addEventListener('beforeunload', beforeUnload));
onUnmounted(() => window.removeEventListener('beforeunload', beforeUnload));
</script>
<template>
  <div class="workspace">
    <a class="skip-link" href="#main-content">跳转到主要内容</a>
    <aside class="sidebar" :class="{ open: menuOpen }">
      <RouterLink class="brand" to="/jobs" @click="menuOpen = false"
        ><span class="brand-mark" aria-hidden="true"><span></span><span></span><span></span></span
        ><span>帧流<small>FRAMEFLOW</small></span></RouterLink
      >
      <nav aria-label="主导航">
        <div v-for="group in groups" :key="group.label" class="nav-group">
          <span class="nav-label">{{ group.label }}</span>
          <template v-for="link in group.links" :key="link.url">
            <RouterLink
              :to="link.url"
              :class="{
                active: route.path === link.url || (link.url === '/jobs' && route.name === 'job'),
              }"
              @click="menuOpen = false"
              ><AppIcon :name="link.icon" /><span>{{ link.label }}</span
              ><span class="nav-active-dot"></span
            ></RouterLink>
          </template>
        </div>
        <RouterLink
          class="delivery-nav"
          aria-label="交付文件"
          to="/delivery-files"
          @click="menuOpen = false"
          >交付文件 ↗</RouterLink
        >
      </nav>
      <div class="sidebar-footer">
        <span class="footer-symbol"><AppIcon name="check" /></span>
        <div>视频交付工作区<small>任务 · 下载 · 交付</small></div>
      </div>
    </aside>
    <button
      v-if="menuOpen"
      class="menu-backdrop"
      aria-label="关闭导航"
      @click="menuOpen = false"
    ></button>
    <div class="main" id="main-content" tabindex="-1">
      <header class="topbar">
        <div class="actions">
          <button
            type="button"
            class="mobile-menu"
            aria-label="打开导航"
            :aria-expanded="menuOpen"
            @click="menuOpen = !menuOpen"
          >
            <AppIcon name="menu" /></button
          ><span class="breadcrumb"
            >工作区 <span class="breadcrumb-divider">/</span
            ><strong>{{ route.meta.title }}</strong></span
          >
        </div>
        <div class="actions">
          <RouterLink class="console-identity" to="/login"
            ><span class="user-avatar"><AppIcon name="users" /></span
            ><span>管理账户</span></RouterLink
          ><button class="logout-button" type="button" @click="logout">
            <AppIcon name="logout" /><span>退出登录</span>
          </button>
        </div>
      </header>
      <p v-if="error" class="notice error" role="alert">{{ error }}</p>
      <slot />
    </div>
  </div>
</template>
