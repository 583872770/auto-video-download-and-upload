<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue';
import { RouterLink } from 'vue-router';
import { api } from '../../api';
import { useOperation } from '../../composables/operation';
import { confirmNavigation, useLeaveGuard } from '../../composables/leaveGuard';
interface User {
  username: string;
  role: string;
  enabled: boolean;
}
interface Status {
  configured: boolean;
  user: User | null;
  local_access: boolean;
  bootstrap_path: string;
}
const state = ref<Status>(),
  users = ref<User[]>([]),
  op = useOperation();
const login = reactive({ username: '', password: '', bootstrap_key: '' });
const create = reactive({ username: '', password: '', role: 'viewer' });
const edit = reactive({ username: '', password: '', role: 'viewer', enabled: true });
const editBaseline = ref(JSON.stringify(edit)),
  navigating = ref(false);
const roles: Record<string, string> = { admin: '管理员', operator: '操作员', viewer: '只读' };
const dirty = computed(
  () =>
    !!(
      login.password ||
      login.bootstrap_key ||
      create.username ||
      create.password ||
      create.role !== 'viewer' ||
      JSON.stringify(edit) !== editBaseline.value
    ),
);
useLeaveGuard(() => ({
  dirty: !navigating.value && dirty.value,
  busy: !navigating.value && op.busy.value,
}));
async function load() {
  state.value = await api.request<Status>('/api/auth/status');
  if (state.value.user?.role === 'admin')
    users.value = (await api.request<{ items: User[] }>('/api/auth/users')).items;
}
function choose() {
  const user = users.value.find((u) => u.username === edit.username);
  if (user) {
    Object.assign(edit, user, { enabled: !!user.enabled, password: '' });
    editBaseline.value = JSON.stringify(edit);
  }
}
async function authenticate() {
  await op.run(async () => {
    const result = await api.request<{ redirect: string }>(
      '/api/auth/' + (state.value?.configured ? 'login' : 'setup'),
      { method: 'POST', body: login },
    );
    login.password = '';
    login.bootstrap_key = '';
    navigating.value = true;
    location.assign(result.redirect);
  });
}
async function logout() {
  if (!(await confirmNavigation())) return;
  await op.run(async () => {
    await api.request('/api/auth/logout', { method: 'POST', body: {} });
    navigating.value = true;
    location.assign('/login');
  });
}
async function save(adding: boolean) {
  await op.run(async () => {
    const body = adding
      ? { ...create }
      : {
          role: edit.role,
          enabled: edit.enabled,
          ...(edit.password ? { password: edit.password } : {}),
        };
    const result = await api.request<{ message: string }>(
      '/api/auth/users' + (adding ? '' : '/' + encodeURIComponent(edit.username)),
      { method: adding ? 'POST' : 'PUT', body },
    );
    if (adding) Object.assign(create, { username: '', password: '', role: 'viewer' });
    else edit.password = '';
    await load();
    if (!adding) editBaseline.value = JSON.stringify(edit);
    op.message.value = result.message;
  });
}
onMounted(() => op.run(load));
</script>
<template>
  <main class="content access-page" :class="{ 'login-access': !state?.user }">
    <RouterLink class="access-brand" to="/jobs">帧流 FRAMEFLOW</RouterLink>
    <div class="access-body">
      <div class="page-heading">
        <div>
          <h1>{{ state?.user ? '管理账户' : '登录管理账户' }}</h1>
          <p>
            {{
              state?.user
                ? '管理工作区账户与访问权限。'
                : '使用管理账户访问客户任务、交付和系统设置。'
            }}
          </p>
        </div>
        <RouterLink v-if="state?.user || state?.local_access" to="/jobs">进入工作区</RouterLink>
      </div>
      <p v-if="op.error.value" class="notice error" role="alert">{{ op.error.value }}</p>
      <p v-if="op.message.value" class="notice" role="status">{{ op.message.value }}</p>
      <section v-if="!state" class="card">
        <button :disabled="op.busy.value" @click="op.run(load)">重新读取登录状态</button>
      </section>
      <form v-else-if="!state.user" class="card form-grid auth-form" @submit.prevent="authenticate">
        <h2>{{ state.configured ? '登录' : '初始化管理员' }}</h2>
        <p v-if="!state.configured">
          初始设置码位于 {{ state.bootstrap_path }}。{{
            state.local_access ? '当前允许本机临时访问。' : ''
          }}
        </p>
        <label
          >用户名<input
            v-model="login.username"
            autocomplete="username"
            required
            minlength="3"
            maxlength="64"
        /></label>
        <label
          >密码<input
            v-model="login.password"
            type="password"
            autocomplete="current-password"
            required
            :minlength="state.configured ? undefined : 10"
            maxlength="256"
        /></label>
        <label v-if="!state.configured"
          >初始设置码<input
            v-model="login.bootstrap_key"
            type="password"
            autocomplete="off"
            required
        /></label>
        <button class="primary" :disabled="op.busy.value">
          {{ state.configured ? '登录' : '创建管理员' }}
        </button>
      </form>
      <template v-else
        ><section class="card">
          <h2>{{ state.user.username }} · {{ roles[state.user.role] }}</h2>
          <button :disabled="op.busy.value" @click="logout">退出登录</button>
        </section>
        <template v-if="state.user.role === 'admin'">
          <section class="card">
            <h2>已有账户</h2>
            <p v-for="user in users" :key="user.username">
              {{ user.username }} · {{ roles[user.role] }} · {{ user.enabled ? '启用' : '停用' }}
            </p>
          </section>
          <form class="card form-grid" @submit.prevent="save(true)">
            <h2>创建管理账户</h2>
            <label
              >用户名<input
                v-model="create.username"
                required
                minlength="3"
                maxlength="64"
                autocomplete="off" /></label
            ><label
              >密码<input
                v-model="create.password"
                type="password"
                required
                minlength="10"
                maxlength="256"
                autocomplete="new-password" /></label
            ><label
              >角色<select v-model="create.role">
                <option v-for="(label, key) in roles" :key="key" :value="key">{{ label }}</option>
              </select></label
            ><button :disabled="op.busy.value">创建账户</button>
          </form>
          <form class="card form-grid" @submit.prevent="save(false)">
            <h2>更新管理账户</h2>
            <label
              >用户名<select v-model="edit.username" required @change="choose">
                <option value="" disabled>选择账户</option>
                <option v-for="user in users" :key="user.username">{{ user.username }}</option>
              </select></label
            ><label
              >角色<select v-model="edit.role">
                <option v-for="(label, key) in roles" :key="key" :value="key">{{ label }}</option>
              </select></label
            ><label><input v-model="edit.enabled" type="checkbox" />启用账户</label
            ><label
              >新密码<input
                v-model="edit.password"
                type="password"
                minlength="10"
                maxlength="256"
                autocomplete="new-password"
                placeholder="留空保留密码"
            /></label>
            <p>更新会使该账户已有登录会话失效；至少保留一个启用的管理员。</p>
            <button :disabled="!edit.username || op.busy.value">更新账户</button>
          </form>
        </template>
      </template>
    </div>
  </main>
</template>
