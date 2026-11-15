<script setup lang="ts">
import ScopePanel from '../../components/ScopePanel.vue';
import { computed, onMounted, onUnmounted, reactive, ref } from 'vue';
import { useRoute } from 'vue-router';
import { api } from '../../api';
import InstancePicker from '../../components/InstancePicker.vue';
import AppIcon from '../../components/AppIcon.vue';
import { useInstances } from '../../stores/instances';
import { useResource } from '../../composables/resource';
import { usePolling } from '../../composables/polling';
import { useOperation } from '../../composables/operation';
import AccountDetail from './AccountDetail.vue';
import AccountImport from './AccountImport.vue';
import AccountPacing from './AccountPacing.vue';
import AccountParticipation from './AccountParticipation.vue';
import AccountBatch from './AccountBatch.vue';
import {
  base,
  authLabels,
  downloadLabels,
  type Overview,
  type AccountBatchSummary,
  type Account,
} from './contracts';
const route = useRoute(),
  instances = useInstances(),
  resource = useResource<Overview>(),
  op = useOperation();
const filter = reactive({
    q: '',
    instance: '',
    auth: '',
    download: '',
    linked: '',
    pooled: '',
    status: 'all',
  }),
  page = ref(1),
  tab = ref(route.query.tab === 'pacing' ? 'pacing' : 'library'),
  selected = ref<string | null>(null),
  importing = ref(false),
  ready = ref(false),
  pending = ref(false),
  admin = ref(false),
  capabilities = ref<{ encryption_ready: boolean; message: string }>();
const checked = ref<Account[]>([]),
  bulk = ref<'login' | 'bind' | 'check' | null>(null),
  resumeBatch = ref(false),
  batch = ref<AccountBatchSummary>(),
  checkBatch = ref<AccountBatchSummary>(),
  participation = ref<'join' | 'remove' | null>(null);
const now = ref(Date.now() / 1000);
const timer = window.setInterval(() => {
  now.value = Date.now() / 1000;
}, 1000);
onUnmounted(() => window.clearInterval(timer));
const batchItems = ref<Account[]>([]);
const statusTabs = [
  ['all', '全部账号'],
  ['valid', '已验证登录'],
  ['needs_login', '未登录 / 失效'],
  ['cooldown', '冷却中'],
  ['attention', '需人工处理'],
  ['unchecked', '待确认登录'],
];
const batches = computed(() =>
  [
    { mode: 'login' as const, title: '批量自动登录', value: batch.value },
    { mode: 'check' as const, title: '批量检测登录', value: checkBatch.value },
  ].filter((row) => row.value?.id),
);
function cooldown(item: Account) {
  const until = item.cooldown_until;
  const seconds = Math.max(0, Math.ceil(until - now.value));
  if (!until) return '等待实例更新冷却时间';
  if (!seconds) return '冷却到期，等待恢复检查';
  return `${Math.floor(seconds / 3600) ? Math.floor(seconds / 3600) + '时 ' : ''}${Math.floor((seconds % 3600) / 60)}分 ${String(seconds % 60).padStart(2, '0')}秒后检查恢复`;
}
function checkedAt(item: Account) {
  const stamp = Math.max(0, ...item.cookies.map((cookie) => cookie.auth.checked_at || 0));
  return stamp
    ? new Date(stamp * 1000).toLocaleString('zh-CN', {
        month: '2-digit',
        day: '2-digit',
        hour: '2-digit',
        minute: '2-digit',
      })
    : '尚未检测';
}
function statusTone(item: Account) {
  return item.auth_state === 'valid'
    ? 'success'
    : ['unlinked', 'unchecked', 'expired', 'unconfirmed'].includes(item.auth_state)
      ? 'neutral'
      : 'warning';
}
function switchTab(value: string) {
  if (!pending.value && tab.value !== value) {
    ready.value = false;
    tab.value = value;
  }
}
async function showPacing(target: string) {
  await op.run(async () => {
    await instances.load();
    if (instances.error) throw new Error(instances.error);
    if (instances.items.length && !instances.items.some((node) => node.id === target))
      throw new Error('账号所属实例已不在可管理清单中，请刷新后重试。');
    if (instances.items.some((node) => node.id === target)) instances.select(target);
    selected.value = null;
    switchTab('pacing');
  });
}
function quickStatus(key: string) {
  filter.status = key;
  filter.auth = '';
  filter.download = '';
  search();
}
const checkedIds = computed(() => new Set(checked.value.map((row) => row.id)));
const currentRows = computed(() => resource.data.value?.items || []);
const allChecked = computed(
  () =>
    currentRows.value.length > 0 && currentRows.value.every((row) => checkedIds.value.has(row.id)),
);
const partlyChecked = computed(
  () => !allChecked.value && currentRows.value.some((row) => checkedIds.value.has(row.id)),
);
function toggle(row: Account) {
  checked.value = checkedIds.value.has(row.id)
    ? checked.value.filter((item) => item.id !== row.id)
    : [...checked.value, { ...row }];
}
function togglePage() {
  if (allChecked.value)
    checked.value = checked.value.filter(
      (row) => !currentRows.value.some((item) => item.id === row.id),
    );
  else for (const row of currentRows.value) if (!checkedIds.value.has(row.id)) toggle(row);
}
function openBatch(mode: 'login' | 'bind' | 'check', resume = false, rows = checked.value) {
  batchItems.value = [...rows];
  resumeBatch.value = resume;
  bulk.value = mode;
}
async function refreshBatch(signal?: AbortSignal) {
  const [current, checking] = await Promise.all([
    api.request<AccountBatchSummary>(base + '/batch-login?compact=1', { signal }),
    api.request<AccountBatchSummary>(base + '/batch-check?compact=1', { signal }),
  ]);
  if (!signal?.aborted) {
    batch.value = current;
    checkBatch.value = checking;
  }
}
async function batchChanged() {
  const processed = new Set((bulk.value ? batchItems.value : checked.value).map((row) => row.id));
  checked.value = checked.value.filter((row) => !processed.has(row.id));
  await Promise.all([refresh(), refreshBatch()]);
}
async function refresh(signal?: AbortSignal) {
  await resource.load(
    (s) =>
      api.request(
        base +
          '/overview?' +
          new URLSearchParams({ ...filter, page: String(page.value), size: '50' }),
        { signal: s },
      ),
    signal,
  );
}
function search() {
  page.value = 1;
  void refresh();
}
function change() {
  ready.value = true;
}
async function saved() {
  await refresh();
}
onMounted(() =>
  op.run(async () => {
    const [c, s] = await Promise.all([
      api.request<{ encryption_ready: boolean; message: string }>(base + '/capabilities'),
      api.request<{ user: { role: string } | null }>('/api/auth/status'),
    ]);
    capabilities.value = c;
    admin.value = s.user?.role === 'admin';
  }),
);
usePolling(
  computed(
    () =>
      selected.value === null &&
      !importing.value &&
      !bulk.value &&
      !participation.value &&
      tab.value === 'library',
  ),
  refresh,
  10000,
);
usePolling(
  computed(() => admin.value && !bulk.value),
  refreshBatch,
  5000,
);
</script>
<template>
  <main class="content account-workspace">
    <div class="page-heading">
      <div>
        <span class="eyebrow">ACCOUNT WORKSPACE</span>
        <h1>账号管理</h1>
        <p>统一查看全部账号，管理登录、实例分配和下载参与状态。</p>
      </div>
      <div class="actions">
        <button @click="refresh()">刷新账号</button>
        <button
          :disabled="!admin || pending || !capabilities?.encryption_ready"
          @click="importing = true"
        >
          导入账号
        </button>
        <button
          class="primary"
          :disabled="!admin || pending || !capabilities?.encryption_ready"
          @click="selected = ''"
        >
          <AppIcon name="plus" />新增账号
        </button>
      </div>
    </div>
    <p v-if="op.error.value" class="notice error" role="alert">{{ op.error.value }}</p>
    <p v-if="capabilities && !capabilities.encryption_ready" class="notice">
      {{ capabilities.message }}
    </p>
    <div class="account-page-tabs">
      <button
        :class="{ active: tab === 'library' }"
        :disabled="pending"
        @click="switchTab('library')"
      >
        <AppIcon name="users" />账号清单
      </button>
      <button :class="{ active: tab === 'pacing' }" @click="switchTab('pacing')">请求节奏</button>
      <span>{{ tab === 'library' ? '全部实例的账号集中管理' : '按实例配置，作用于后续任务' }}</span>
    </div>
    <template v-if="tab === 'library'">
      <ScopePanel
        level="global"
        purpose="view"
        :target="
          filter.instance === 'unbound'
            ? '尚未分配的账号'
            : resource.data.value?.instances.find((node) => node.id === filter.instance)?.name ||
              '全部实例的账号'
        "
        description="实例筛选只改变查看范围。批量操作以勾选账号为准，分配实例时另行指定目标。"
      />
      <section class="account-filters card">
        <form class="account-search" @submit.prevent="search">
          <label class="search-field"
            ><span>搜索账号</span
            ><input
              v-model="filter.q"
              aria-label="搜索账号"
              type="search"
              placeholder="搜索邮箱、备注或 Cookie 名称"
          /></label>
          <label
            ><span>查看账号所属实例</span
            ><select v-model="filter.instance" aria-label="按绑定实例筛选" @change="search">
              <option value="">全部实例</option>
              <option value="unbound">尚未分配</option>
              <option
                v-for="node in resource.data.value?.instances"
                :key="node.id"
                :value="node.id"
              >
                {{ node.name }}{{ node.online ? '' : '（离线）' }}
              </option>
            </select></label
          >
          <label
            ><span>参与下载</span
            ><select v-model="filter.pooled" aria-label="账号池筛选" @change="search">
              <option value="">全部</option>
              <option value="1">已加入账号池</option>
              <option value="0">未加入账号池</option>
            </select></label
          >
          <button type="submit">查询</button>
        </form>
        <div class="account-status-tabs" aria-label="账号状态筛选">
          <button
            v-for="[key, label] in statusTabs"
            :key="key"
            :class="[key, { active: filter.status === key }]"
            :aria-pressed="filter.status === key"
            @click="quickStatus(key!)"
          >
            <i></i>{{ label }}<b>{{ resource.data.value?.summary?.[key!] || 0 }}</b>
          </button>
        </div>
      </section>
      <section class="card account-list">
        <div v-for="entry in batches" :key="entry.mode" class="account-batch-banner">
          <div>
            <strong>{{ entry.title }}{{ entry.value?.active ? '进行中' : ' · 最近结果' }}</strong
            ><span
              >已处理 {{ entry.value?.finished }} / {{ entry.value?.total }} · 成功
              {{ entry.value?.counts.succeeded || 0 }} · 失败
              {{ entry.value?.counts.failed || 0 }} · 需处理
              {{ entry.value?.counts.needs_attention || 0
              }}<template v-if="entry.mode === 'check'">
                · 跳过 {{ entry.value?.counts.skipped || 0 }}</template
              ><template
                v-if="(entry.value?.counts.cancelled || 0) + (entry.value?.counts.interrupted || 0)"
              >
                · 取消 / 中断
                {{
                  (entry.value?.counts.cancelled || 0) + (entry.value?.counts.interrupted || 0)
                }}</template
              ></span
            >
          </div>
          <button @click="openBatch(entry.mode, true)">
            {{ entry.value?.active ? '查看进度' : '查看结果' }}
          </button>
        </div>
        <div class="account-list-heading">
          <div>
            <h2>
              账号清单 <span>{{ resource.data.value?.total || 0 }}</span>
            </h2>
            <p>登录状态与下载状态分别显示；冷却中的账号仍可保留有效登录凭据。</p>
          </div>
          <span v-if="resource.loading.value" class="muted" role="status">正在更新…</span>
        </div>
        <div v-if="admin" class="account-bulk-toolbar" :class="{ 'has-selection': checked.length }">
          <div>
            <strong>已选 {{ checked.length }} 个账号</strong><span>支持跨页选择</span
            ><button v-if="checked.length" @click="checked = []">清空选择</button>
          </div>
          <div class="actions">
            <button :disabled="!checked.length || pending" @click="openBatch('bind')">
              批量修改实例
            </button>
            <button
              :disabled="!checked.length || pending || checkBatch?.active"
              @click="openBatch('check')"
            >
              批量检测登录
            </button>
            <button :disabled="!checked.length || pending" @click="participation = 'join'">
              加入下载
            </button>
            <button :disabled="!checked.length || pending" @click="participation = 'remove'">
              移出下载
            </button>
            <button
              class="primary"
              :disabled="
                !checked.length || !capabilities?.encryption_ready || batch?.active || pending
              "
              @click="openBatch('login')"
            >
              批量自动登录
            </button>
          </div>
        </div>
        <p v-if="!admin" class="muted">登录管理员后可管理账号和执行批量操作。</p>
        <p v-if="resource.error.value" class="notice error" role="alert">
          {{ resource.error.value }}
        </p>
        <div class="table-scroll">
          <table class="account-table">
            <thead>
              <tr>
                <th v-if="admin" class="account-selection-cell">
                  <input
                    type="checkbox"
                    aria-label="全选本页账号"
                    :checked="allChecked"
                    :indeterminate="partlyChecked"
                    :disabled="!currentRows.length"
                    @change="togglePage"
                  />
                </th>
                <th>账号</th>
                <th>所属实例</th>
                <th>登录状态</th>
                <th>下载状态</th>
                <th>参与下载</th>
                <th class="account-actions-cell">操作</th>
              </tr>
            </thead>
            <tbody>
              <tr
                v-for="item in currentRows"
                :key="item.id"
                :class="{ checked: checkedIds.has(item.id) }"
              >
                <td v-if="admin" class="account-selection-cell">
                  <input
                    type="checkbox"
                    :aria-label="'选择账号 ' + item.email"
                    :checked="checkedIds.has(item.id)"
                    @change="toggle(item)"
                  />
                </td>
                <td class="account-identity-cell">
                  <button class="account-email" @click="selected = item.id">{{ item.email }}</button
                  ><small v-if="item.note" class="cell-note account-note" :title="item.note">{{
                    item.note
                  }}</small>
                </td>
                <td class="account-instance-cell">
                  <span v-if="item.instance_id" class="account-instance"
                    ><i :class="{ online: item.online }"></i
                    >{{ item.instance_name || item.instance_id }}</span
                  ><span v-else class="account-unassigned">尚未分配</span
                  ><small v-if="item.instance_id" class="cell-note">{{
                    item.online ? '实例在线' : '实例离线'
                  }}</small>
                </td>
                <td class="account-auth-cell">
                  <span class="account-state" :class="statusTone(item)">{{
                    authLabels[item.auth_state] || '待确认'
                  }}</span
                  ><small class="cell-note">{{ checkedAt(item) }}</small>
                </td>
                <td class="account-download-cell">
                  <span
                    class="account-state"
                    :class="
                      item.download_state === 'cooldown'
                        ? 'cooling'
                        : item.download_state === 'manual'
                          ? 'warning'
                          : item.download_state === 'ready'
                            ? 'success'
                            : 'neutral'
                    "
                    >{{ downloadLabels[item.download_state] || item.download_state }}</span
                  ><small v-if="item.cooling" class="cell-note cooldown-timer">{{
                    cooldown(item)
                  }}</small
                  ><small v-else-if="item.cookies[0]?.session?.active" class="cell-note"
                    >执行中 {{ item.cookies[0].session.active }} /
                    {{ item.cookies[0].session.limit }}</small
                  ><small
                    v-else-if="item.cookies[0]?.auth.message && item.auth_state !== 'valid'"
                    class="cell-note account-reason"
                    :title="item.cookies[0].auth.message"
                    >{{ item.cookies[0].auth.message }}</small
                  >
                </td>
                <td class="account-participation-cell">
                  <span
                    :class="{ participating: item.cookies.some((c) => c.pooled || c.active) }"
                    >{{
                      item.cookies.some((c) => c.pooled)
                        ? '账号池'
                        : item.cookies.some((c) => c.active)
                          ? '单独使用'
                          : '未参与'
                    }}</span
                  >
                </td>
                <td class="account-actions-cell">
                  <button :disabled="pending" @click="selected = item.id">
                    {{ admin ? '管理' : '详情' }}</button
                  ><button
                    v-if="admin"
                    class="account-row-login"
                    :disabled="
                      pending ||
                      batch?.active ||
                      !capabilities?.encryption_ready ||
                      (!!item.instance_id && !item.online)
                    "
                    @click="openBatch('login', false, [item])"
                  >
                    {{ item.cookie_count ? '重新登录' : '登录' }}
                  </button>
                </td>
              </tr>
              <tr v-if="resource.data.value && !currentRows.length">
                <td :colspan="admin ? 7 : 6" class="empty">
                  暂无符合条件的账号。可以切换状态或实例筛选，也可以导入账号。
                </td>
              </tr>
            </tbody>
          </table>
        </div>
        <div class="pager">
          <span
            >共 {{ resource.data.value?.total || 0 }} 个账号 · 第 {{ page }} /
            {{ Math.max(1, Math.ceil((resource.data.value?.total || 0) / 50)) }} 页</span
          ><button
            :disabled="page <= 1 || resource.loading.value"
            @click="
              page--;
              refresh();
            "
          >
            上一页</button
          ><button
            :disabled="
              !resource.data.value ||
              page * 50 >= resource.data.value.total ||
              resource.loading.value
            "
            @click="
              page++;
              refresh();
            "
          >
            下一页
          </button>
        </div>
      </section>
    </template>
    <div v-if="tab === 'pacing'" class="account-pacing-container">
      <div class="account-pacing-scope">
        <InstancePicker :disabled="pending" @change="change" />
      </div>
      <AccountPacing
        v-if="ready"
        :key="'pacing:' + instances.epoch"
        :admin="admin"
        @pending="pending = $event"
        @saved="saved"
      />
    </div>
    <AccountDetail
      v-if="selected !== null"
      :key="selected"
      :id="selected || undefined"
      :nodes="resource.data.value?.instances || []"
      :admin="admin"
      :blocked="pending"
      @close="selected = null"
      @saved="saved"
      @pacing="showPacing"
    />
    <AccountImport v-if="importing" @close="importing = false" @saved="saved" />
    <AccountBatch
      v-if="bulk"
      :mode="bulk"
      :items="batchItems"
      :nodes="resource.data.value?.instances || []"
      :resume="resumeBatch"
      @close="
        bulk = null;
        refreshBatch();
        refresh();
      "
      @changed="batchChanged"
    />
    <AccountParticipation
      v-if="participation"
      :mode="participation"
      :items="checked"
      :nodes="resource.data.value?.instances || []"
      @close="participation = null"
      @changed="batchChanged"
    />
  </main>
</template>
<style scoped>
.account-page-tabs {
  display: flex;
  gap: 26px;
  align-items: center;
  border-bottom: 1px solid var(--line);
  margin-bottom: 20px;
}
.account-page-tabs button {
  display: flex;
  gap: 8px;
  align-items: center;
  padding: 13px 0;
  border: 0;
  border-radius: 0;
  border-bottom: 2px solid transparent;
  background: transparent;
  color: var(--muted);
}
.account-page-tabs button.active {
  border-bottom-color: #178578;
  color: #167d70;
  font-weight: 600;
}
.account-page-tabs > span {
  margin-left: auto;
  color: var(--muted);
  font-size: 11px;
}
.account-filters {
  padding: 18px 20px 0;
  margin-bottom: 16px;
}
.account-search {
  display: flex;
  align-items: flex-end;
  gap: 14px;
  margin-bottom: 20px;
}
.account-search label {
  display: flex;
  flex-direction: column;
  gap: 7px;
  font-size: 11px;
  color: var(--muted);
  min-width: 155px;
}
.account-search .search-field {
  flex: 1;
}
.account-search input,
.account-search select {
  width: 100%;
  height: 36px;
  min-width: 0;
}
.account-status-tabs {
  display: flex;
  gap: 9px;
  flex-wrap: wrap;
  border-top: 1px solid #eef2f5;
  padding: 15px 0;
}
.account-status-tabs button {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  border-color: transparent;
  background: #f6f8fa;
  color: #64748b;
  font-size: 12px;
  padding: 7px 11px;
}
.account-status-tabs b {
  padding: 1px 6px;
  border-radius: 4px;
  background: #e9edf2;
  font-size: 11px;
  font-weight: 500;
}
.account-status-tabs button.active {
  background: #edf8f5;
  border-color: #bbdfd7;
  color: #147d6e;
}
.account-status-tabs button.active b {
  background: #d4eee7;
}
.account-status-tabs i {
  height: 6px;
  width: 6px;
  border-radius: 50%;
  background: #a1aeba;
}
.account-status-tabs .valid i {
  background: #259f71;
}
.account-status-tabs .cooldown i {
  background: #d99931;
}
.account-status-tabs .attention i {
  background: #d47058;
}
.account-list {
  padding: 0;
  overflow: hidden;
}
.account-list-heading {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 20px 20px 15px;
}
.account-list-heading h2 {
  font-size: 14px;
  margin: 0;
}
.account-list-heading h2 span {
  color: var(--muted);
  font-size: 12px;
  margin-left: 6px;
  font-weight: 400;
}
.account-list-heading p {
  font-size: 11px;
  color: var(--muted);
  margin: 6px 0 0;
}
.account-bulk-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 12px;
  padding: 12px 20px;
  background: #f8fafb;
  border-block: 1px solid #e9eef2;
}
.account-bulk-toolbar.has-selection {
  background: #f0f8f5;
}
.account-bulk-toolbar > div:first-child {
  display: flex;
  align-items: center;
  gap: 10px;
}
.account-bulk-toolbar strong {
  font-size: 12px;
  font-weight: 500;
}
.account-bulk-toolbar span {
  color: var(--muted);
  font-size: 10px;
}
.account-bulk-toolbar > div:first-child > button {
  border: 0;
  background: transparent;
  color: #168578;
  padding: 3px;
  font-size: 11px;
}
.account-bulk-toolbar .actions {
  gap: 7px;
}
.account-bulk-toolbar .actions button {
  font-size: 11px;
  padding: 6px 9px;
  min-height: 30px;
}
.account-table {
  margin: 0;
  min-width: 1000px;
}
.account-table th {
  font-size: 11px;
  font-weight: 500;
  color: #718096;
  background: #fcfdfe;
  padding: 12px 14px;
}
.account-table td {
  padding: 15px 14px;
  vertical-align: middle;
  font-size: 12px;
}
.account-table tr.checked td {
  background: #f5fbf8;
}
.account-table .account-selection-cell {
  width: 42px;
  padding-right: 0;
  text-align: center;
}
.account-selection-cell input {
  vertical-align: middle;
}
.account-workspace .account-table button.account-email {
  display: block;
  border: 0;
  background: transparent;
  font-weight: 500;
  padding: 0;
  min-height: unset;
  text-align: left;
  color: #26364a;
  font-size: 12px;
  overflow-wrap: anywhere;
}
.account-note {
  max-width: 230px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.account-table .cell-note {
  font-size: 10px;
  margin-top: 5px;
  color: #8995a4;
}
.account-instance {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}
.account-instance i {
  width: 5px;
  height: 5px;
  background: #b5bdc7;
  border-radius: 50%;
}
.account-instance i.online {
  background: #37a77c;
}
.account-unassigned {
  color: #9aa5b1;
}
.account-state {
  display: inline-flex;
  padding: 4px 7px;
  border-radius: 4px;
  font-size: 11px;
  white-space: nowrap;
}
.account-state.success {
  color: #208562;
  background: #eaf7f0;
}
.account-state.neutral {
  color: #7b8797;
  background: #f1f4f7;
}
.account-state.warning {
  color: #b05e41;
  background: #fff0e9;
}
.account-state.cooling {
  color: #ad7a24;
  background: #fff5de;
}
.account-table .cooldown-timer {
  color: #ad7a24;
  font-variant-numeric: tabular-nums;
}
.account-reason {
  max-width: 200px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.participating {
  color: #18816d;
}
.account-actions-cell {
  white-space: nowrap;
}
.account-actions-cell button {
  font-size: 11px;
  padding: 4px 8px;
  min-height: 28px;
  margin-right: 5px;
}
.account-actions-cell .account-row-login {
  color: #178578;
  border-color: transparent;
  background: transparent;
}
.pager {
  padding: 15px 20px;
  border-top: 1px solid var(--line);
  margin: 0;
}
.account-list > .notice {
  margin: 12px 20px;
}
.account-batch-banner {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  background: #f1f8f5;
  border-bottom: 1px solid #dfede6;
  padding: 11px 20px;
}
.account-batch-banner > div {
  display: flex;
  gap: 12px;
  flex-wrap: wrap;
}
.account-batch-banner strong {
  font-size: 12px;
  color: #178578;
  font-weight: 500;
}
.account-batch-banner span {
  font-size: 11px;
  color: var(--muted);
}
.account-batch-banner button {
  font-size: 11px;
  white-space: nowrap;
}
.account-pacing-scope {
  display: flex;
  justify-content: flex-end;
  margin-bottom: 16px;
}
.account-workspace .primary {
  background: #178578;
  border-color: #178578;
  color: white;
}
.account-workspace .page-heading .actions button.primary {
  background: #178578;
  border-color: #178578;
}
@media (max-width: 800px) {
  .account-search {
    flex-wrap: wrap;
  }
  .account-search .search-field {
    flex-basis: 100%;
  }
  .account-search label {
    flex: 1;
    min-width: 120px;
  }
  .account-page-tabs > span {
    display: none;
  }
  .account-bulk-toolbar > div:first-child > span {
    display: none;
  }
  .account-bulk-toolbar .actions {
    flex-wrap: wrap;
  }
  .account-batch-banner > div {
    gap: 5px;
  }
  .account-list-heading p {
    max-width: 320px;
    line-height: 1.6;
  }
}

@media (max-width: 600px) {
  .account-table {
    min-width: 0;
  }
  .account-table thead,
  .account-table tbody {
    display: block;
  }
  .account-table thead tr {
    display: block;
  }
  .account-table th {
    display: none;
  }
  .account-table th.account-selection-cell {
    display: block;
    width: auto;
    padding: 12px 16px;
    text-align: left;
  }
  .account-table th.account-selection-cell::after {
    content: '全选本页';
    margin-left: 10px;
  }
  .account-table tbody tr {
    position: relative;
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 0 8px;
    padding: 14px 16px;
    border-bottom: 1px solid var(--line);
  }
  .account-table tbody tr:last-child {
    border-bottom: 0;
  }
  .account-table td {
    display: block;
    padding: 7px 0;
    border: 0;
    min-width: 0;
  }
  .account-table td.account-selection-cell {
    position: absolute;
    right: 16px;
    top: 16px;
    width: auto;
    padding: 0;
  }
  .account-table td.account-identity-cell {
    grid-column: 1 / -1;
    padding-right: 26px;
  }
  .account-table .account-email {
    overflow-wrap: anywhere;
  }
  .account-table .account-instance-cell::before {
    content: '所属实例';
  }
  .account-table .account-auth-cell::before {
    content: '登录状态';
  }
  .account-table .account-download-cell::before {
    content: '下载状态';
  }
  .account-table .account-participation-cell::before {
    content: '参与下载';
  }
  .account-table td::before {
    display: block;
    font-size: 10px;
    color: #94a3b8;
    margin-bottom: 6px;
  }
  .account-table td.account-actions-cell {
    grid-column: 1 / -1;
    display: flex;
    justify-content: flex-end;
    padding-bottom: 0;
  }
  .account-table td.empty {
    grid-column: 1 / -1;
    padding: 20px 0;
  }
  .account-table .account-reason {
    white-space: normal;
  }
  .account-note {
    max-width: 100%;
  }
  .account-list-heading p {
    font-size: 10px;
  }
}
</style>
