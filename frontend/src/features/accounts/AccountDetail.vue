<script setup lang="ts">
import { computed, onMounted, onUnmounted, reactive, ref } from 'vue';
import { api } from '../../api';
import AppIcon from '../../components/AppIcon.vue';
import WorkspaceDialog from '../../components/WorkspaceDialog.vue';
import { useOperation } from '../../composables/operation';
import { useResource } from '../../composables/resource';
import { useLeaveGuard } from '../../composables/leaveGuard';
import AccountLogin from './AccountLogin.vue';
import SessionProgress from './SessionProgress.vue';
import {
  base,
  emailCodeOrigin,
  authLabels,
  downloadLabels,
  at,
  type Account,
  type Cookie,
  type CookieEvent,
  type Node,
} from './contracts';
const props = defineProps<{ id?: string; nodes: Node[]; admin: boolean; blocked?: boolean }>(),
  emit = defineEmits<{ close: []; saved: []; pacing: [instance: string] }>();
const op = useOperation(),
  item = ref<Account>(),
  loaded = ref(false),
  baseline = ref(''),
  tab = ref('profile'),
  login = ref(false),
  upload = ref<File>(),
  confirmed = ref(false);
const form = reactive({
  email: '',
  password: '',
  extra: '',
  note: '',
  instance_id: '',
});
const dirty = computed(
  () => (!!baseline.value && JSON.stringify(form) !== baseline.value) || !!upload.value,
);
const events = useResource<CookieEvent[]>(),
  kind = ref('');
const eventLabels: Record<string, string> = {
  interval_change: '间隔调整',
  recovery: '恢复阶段',
  failure: '异常与退避',
  auth_check: '登录检测',
};
const filteredEvents = computed(() =>
  (events.data.value || []).filter((event) => !kind.value || event.event_type === kind.value),
);
useLeaveGuard(() => ({ dirty: dirty.value, busy: op.busy.value }));
async function beforeClose() {
  return !dirty.value || window.confirm('放弃此账号未保存的修改？');
}
function fill() {
  Object.assign(form, {
    email: item.value?.email || '',
    note: item.value?.note || '',
    instance_id: item.value?.instance_id || '',
    password: '',
    extra: '',
  });
  baseline.value = JSON.stringify(form);
  upload.value = undefined;
  confirmed.value = false;
}
async function read() {
  loaded.value = false;
  const id = item.value?.id || props.id;
  if (id) item.value = (await api.request<{ item: Account }>(base + '/' + id)).item;
  fill();
  if (id && props.admin) {
    const [password, extra] = await Promise.all(
      ['password', 'extra'].map((field) =>
        api.request<{ value: string }>(base + '/' + id + '/secrets', {
          method: 'POST',
          body: { field },
        }),
      ),
    );
    if (!alive) return;
    form.password = password!.value;
    form.extra = extra!.value;
    baseline.value = JSON.stringify(form);
  }
  loaded.value = true;
}
async function close() {
  if (await beforeClose()) emit('close');
}
async function reload() {
  if (!(await beforeClose())) return;
  await op.run(read);
}
async function save() {
  await op.run(async () => {
    const body = {
      email: form.email,
      note: form.note,
      instance_id: form.instance_id,
      password: form.password,
      extra: form.extra,
      ...(item.value ? { version: item.value.version } : {}),
    };
    const value = await api.request<{ id?: string; message: string }>(
      base + (item.value ? '/' + item.value.id : ''),
      { method: item.value ? 'PATCH' : 'POST', body },
    );
    const id = item.value?.id || value.id;
    item.value = (await api.request<{ item: Account }>(base + '/' + id)).item;
    await read();
    op.message.value = value.message;
    emit('saved');
  });
}
async function remove() {
  if (
    !item.value ||
    !window.confirm(
      `删除 ${item.value.email} 的资料、密钥与 Cookie？历史记录和实例归属保留，在用时会拒绝删除。`,
    )
  )
    return;
  await op.run(async () => {
    await api.request(base + '/' + item.value!.id, {
      method: 'DELETE',
      body: { version: item.value!.version },
    });
    baseline.value = JSON.stringify(form);
    upload.value = undefined;
    emit('saved');
    emit('close');
  });
}
function requireOwner(cookie: Cookie) {
  if (dirty.value || props.blocked) throw new Error('请先保存账号或请求节奏修改。');
  return {
    id: cookie.instance_id,
    epoch: 0,
    isCurrent: () => alive,
  };
}
let alive = true;
onUnmounted(() => {
  alive = false;
  Object.assign(form, { password: '', extra: '' });
});
async function cookieAction(cookie: Cookie, action: string) {
  await op.run(async () => {
    if (action === 'check') {
      if (dirty.value) throw new Error('请先保存资料修改。');
      const result = await api.request<{ message: string }>(base + '/cookie-action', {
        method: 'POST',
        body: { action, instance_id: cookie.instance_id, profile_id: cookie.id },
      });
      op.message.value = result.message;
    } else {
      const scope = requireOwner(cookie);
      if (action === 'export') {
        const blob = await api.request<Blob>('/api/youtube/cookies/export', {
          method: 'POST',
          body: { id: cookie.id },
          scope,
          responseType: 'blob',
        });
        const url = URL.createObjectURL(blob),
          link = document.createElement('a');
        link.href = url;
        link.download = 'youtube-cookie-' + cookie.id + '.txt';
        link.click();
        setTimeout(() => URL.revokeObjectURL(url), 1000);
        return;
      }
      if (
        !window.confirm(
          action === 'delete'
            ? `删除 Cookie「${cookie.name}」？`
            : `单独使用「${cookie.name}」会关闭此实例账号池，继续？`,
        )
      )
        return;
      await api.request('/api/youtube/cookies/' + action, {
        method: 'POST',
        scope,
        body: { id: cookie.id },
      });
    }
    await read();
    emit('saved');
  });
}
async function history() {
  await events.load(async (signal) => {
    const rows = await Promise.all(
      (item.value?.cookies || []).map((c) =>
        api.request<{ events: CookieEvent[] }>(
          `${base}/cookies/${encodeURIComponent(c.instance_id)}/${encodeURIComponent(c.id)}/timeline`,
          { signal },
        ),
      ),
    );
    return rows
      .flatMap((r) => r.events)
      .sort((a, b) => b.occurred_at - a.occurred_at)
      .slice(0, 100);
  });
}
async function selectTab(key: string) {
  op.message.value = '';
  tab.value = key;
  if (key === 'schedule') await history();
}
async function uploadCookie() {
  await op.run(async () => {
    if (!upload.value || !confirmed.value || !item.value) return;
    if (upload.value.size > 1024 * 1024) throw new Error('Cookie 文件最大 1 MB。');
    if (JSON.stringify(form) !== baseline.value) throw new Error('请先保存账号资料。');
    const data = new FormData();
    data.append('cookie', upload.value);
    data.append('confirmed', 'true');
    data.append('version', String(item.value.version));
    await api.request(base + '/' + item.value.id + '/upload', { method: 'POST', body: data });
    await read();
    emit('saved');
    op.message.value = 'Cookie 已替换，保留账号池选择与冷却。';
  });
}
onMounted(() => op.run(read));
</script>
<template>
  <WorkspaceDialog
    :open="!login"
    :title="item ? '账号详情' : '新增账号'"
    :busy="op.busy.value"
    :before-close="beforeClose"
    class="account-dialog"
    @close="emit('close')"
  >
    <div class="account-body">
      <header v-if="item" class="account-summary">
        <div class="account-avatar"><AppIcon name="users" /></div>
        <div class="account-summary-text">
          <h3>{{ item.email }}</h3>
          <p>
            {{ item.instance_name || '尚未绑定实例'
            }}<span v-if="item.instance_id"> · {{ item.online ? '在线' : '离线' }}</span>
          </p>
        </div>
        <span class="account-status" :class="{ success: item.auth_state === 'valid' }">
          <i></i>{{ authLabels[item.auth_state] || item.auth_state }}
        </span>
      </header>
      <div class="account-navigation">
        <nav class="account-tabs" aria-label="账号详情页签">
          <button
            v-for="[key, label] in [
              ['profile', '账号资料'],
              ['cookies', '登录与凭据'],
              ['schedule', '运行记录'],
            ]"
            :key="key"
            :class="{ selected: tab === key }"
            :aria-pressed="tab === key"
            :disabled="!item && key !== 'profile'"
            @click="selectTab(key!)"
          >
            {{ label }}
          </button>
        </nav>
        <button v-if="item" class="account-refresh" :disabled="op.busy.value" @click="reload">
          {{ dirty ? '重新读取' : '刷新' }}
        </button>
      </div>
      <p v-if="op.error.value" class="notice error" role="alert">{{ op.error.value }}</p>
      <p v-if="op.message.value" class="notice" role="status">{{ op.message.value }}</p>
      <p v-if="!loaded" class="account-loading" role="status">
        {{ op.busy.value ? '正在读取账号资料…' : '资料读取失败，请刷新后重试。' }}
      </p>
      <form v-if="tab === 'profile'" id="account-profile-form" @submit.prevent="save">
        <fieldset :disabled="!admin || op.busy.value || !loaded" class="account-fields">
          <label
            >邮箱账号<input
              v-model="form.email"
              type="email"
              required
              maxlength="254"
              :disabled="!!item?.cookie_count"
              autocomplete="off"
              placeholder="输入账号邮箱"
          /></label>
          <label v-if="admin"
            >密码<input
              v-model="form.password"
              type="text"
              :required="!item"
              maxlength="16384"
              autocomplete="off"
              spellcheck="false"
              placeholder="输入账号密码"
          /></label>
          <label
            >所属实例<select v-model="form.instance_id" :disabled="!!item?.cookie_count">
              <option value="">登录时选择实例</option>
              <option v-for="node in nodes" :key="node.id" :value="node.id">
                {{ node.name }}{{ node.online ? '' : '（离线）' }}
              </option>
            </select></label
          >
          <div class="account-fixed-field">
            <span>邮件接码</span>
            <div>
              <a :href="emailCodeOrigin" target="_blank" rel="noopener noreferrer"
                >fbvjdjcf.asia <span aria-hidden="true">↗</span></a
              ><span class="account-fixed-label">系统固定</span>
            </div>
          </div>
          <label v-if="admin"
            >附加信息<textarea
              v-model="form.extra"
              maxlength="16384"
              rows="3"
              autocomplete="off"
              spellcheck="false"
              placeholder="未填写附加信息"
            ></textarea>
          </label>
          <label
            >备注<textarea
              v-model="form.note"
              maxlength="4000"
              rows="3"
              placeholder="账号用途或其他备注"
            ></textarea>
          </label>
        </fieldset>
      </form>
      <section v-if="tab === 'cookies'" class="account-pane">
        <div class="account-section-heading">
          <div>
            <h3>登录凭据</h3>
            <p>重新登录成功后替换当前 Cookie，保留账号池与冷却状态。</p>
          </div>
          <button
            v-if="admin && item"
            class="primary"
            :disabled="op.busy.value || dirty || !loaded || (!!item.instance_id && !item.online)"
            @click="login = true"
          >
            {{ item.cookie_count ? '重新登录' : '登录账号' }}<AppIcon name="arrow" />
          </button>
        </div>
        <article v-for="cookie in item?.cookies" :key="cookie.id" class="account-credential">
          <div class="account-credential-heading">
            <strong>当前 Cookie</strong
            ><span class="account-status" :class="{ success: cookie.auth.state === 'valid' }">{{
              authLabels[cookie.auth.state] || cookie.auth.state
            }}</span>
          </div>
          <dl class="account-facts">
            <div>
              <dt>下载状态</dt>
              <dd>{{ downloadLabels[cookie.download_state] || cookie.download_state }}</dd>
            </div>
            <div>
              <dt>账号池</dt>
              <dd>{{ cookie.pooled ? '已加入账号池' : '未加入账号池' }}</dd>
            </div>
            <div>
              <dt>最近检测</dt>
              <dd>{{ at(cookie.auth.checked_at) }}</dd>
            </div>
            <div>
              <dt>最近验证通过</dt>
              <dd>{{ at(cookie.auth.last_valid_at) }}</dd>
            </div>
          </dl>
          <p v-if="cookie.auth.message" class="account-hint">{{ cookie.auth.message }}</p>
          <div v-if="admin" class="account-cookie-actions">
            <button
              :disabled="
                op.busy.value || !cookie.online || cookie.download_state === 'cooldown' || dirty
              "
              @click="cookieAction(cookie, 'check')"
            >
              检测登录
            </button>
            <button
              :disabled="op.busy.value || dirty || blocked"
              @click="cookieAction(cookie, 'export')"
            >
              导出 Cookie
            </button>
            <details class="account-more">
              <summary>更多操作</summary>
              <div class="actions">
                <button
                  :disabled="op.busy.value || dirty || blocked"
                  @click="cookieAction(cookie, 'select')"
                >
                  单独使用
                </button>
                <button
                  class="danger"
                  :disabled="op.busy.value || dirty || blocked"
                  @click="cookieAction(cookie, 'delete')"
                >
                  删除 Cookie
                </button>
              </div>
            </details>
            <button :disabled="dirty || op.busy.value" @click="emit('pacing', cookie.instance_id)">
              请求节奏 ↗
            </button>
          </div>
        </article>
        <div v-if="!item?.cookies.length" class="account-empty">
          <AppIcon name="users" /><strong>尚未保存登录凭据</strong>
          <p>登录账号后自动保存 Cookie，也可以手工导入。</p>
        </div>
        <details v-if="admin && item?.instance_id" class="account-manual">
          <summary>手工导入 Cookie <span>已有浏览器导出的文件时使用</span></summary>
          <form class="account-upload" @submit.prevent="uploadCookie">
            <label
              >Netscape 格式 · TXT · 最大 1 MB<input
                :key="item.version + '-' + !!upload"
                type="file"
                accept=".txt,text/plain"
                :disabled="op.busy.value"
                @change="upload = ($event.target as HTMLInputElement).files?.[0]"
            /></label>
            <label class="account-check"
              ><input v-model="confirmed" type="checkbox" :disabled="op.busy.value" />确认此 Cookie
              属于当前账号，并替换原凭据</label
            >
            <button :disabled="op.busy.value || !upload || !confirmed" class="primary">
              导入并替换
            </button>
          </form>
        </details>
      </section>
      <section v-if="tab === 'schedule'" class="account-pane">
        <div v-for="cookie in item?.cookies" :key="cookie.id" class="account-runtime">
          <SessionProgress :session="cookie.session" :online="cookie.online" />
        </div>
        <div class="account-section-heading">
          <h3>运行事件</h3>
          <div class="actions">
            <select v-model="kind" aria-label="记录类型">
              <option value="">全部记录</option>
              <option v-for="(label, key) in eventLabels" :key="key" :value="key">
                {{ label }}
              </option></select
            ><button
              :disabled="events.loading.value || op.busy.value || dirty"
              @click="
                op.run(async () => {
                  await read();
                  await history();
                })
              "
            >
              刷新记录
            </button>
          </div>
        </div>
        <p v-if="events.error.value" class="notice error" role="alert">{{ events.error.value }}</p>
        <p v-if="events.loading.value" class="account-loading" role="status">正在读取运行记录…</p>
        <ol v-else-if="filteredEvents.length" class="account-timeline">
          <li v-for="(event, index) in filteredEvents" :key="index">
            <div class="account-event-heading">
              <span>{{ eventLabels[event.event_type] || '运行事件' }}</span
              ><time>{{ at(event.occurred_at) }}</time>
            </div>
            <p>{{ event.message || event.kind }}</p>
            <small v-if="event.previous_interval != null"
              >请求间隔 {{ event.previous_interval }} → {{ event.new_interval }} 秒</small
            ><small v-if="event.cooldown_until">冷却截止 {{ at(event.cooldown_until) }}</small>
          </li>
        </ol>
        <div v-else class="account-empty">
          <AppIcon name="logs" /><strong>暂无{{ kind ? '此类' : '' }}运行记录</strong>
          <p>账号的登录检测、冷却和间隔调整会记录在这里。</p>
        </div>
      </section>
    </div>
    <footer class="account-footer">
      <button
        v-if="item && admin && tab === 'profile'"
        class="account-delete"
        :disabled="op.busy.value || !loaded"
        @click="remove"
      >
        删除账号
      </button>
      <span v-else class="account-footer-note">{{
        tab === 'profile' ? '填写资料后即可保存账号' : '每个账号保留一份当前登录凭据'
      }}</span>
      <div class="actions">
        <span v-if="dirty" class="account-unsaved">有未保存的修改</span
        ><button :disabled="op.busy.value" @click="close">{{ dirty ? '取消' : '完成' }}</button
        ><button
          v-if="tab === 'profile' && admin"
          type="submit"
          form="account-profile-form"
          class="primary"
          :disabled="op.busy.value || !loaded"
        >
          {{ op.busy.value ? '处理中…' : item ? '保存修改' : '创建账号' }}
        </button>
      </div>
    </footer>
  </WorkspaceDialog>
  <AccountLogin
    v-if="login && item"
    :item="item"
    :nodes="nodes"
    @close="
      login = false;
      op.run(read);
    "
    @changed="emit('saved')"
  />
</template>
<style scoped>
.account-dialog {
  width: min(860px, calc(100vw - 32px));
  padding: 0;
  border: 0;
  border-radius: 14px;
}
.account-dialog[open] {
  display: flex;
  flex-direction: column;
}
.account-dialog :deep(.modal-heading) {
  padding: 18px 26px;
  margin: 0;
  border-bottom: 1px solid var(--line);
  flex-shrink: 0;
}
.account-dialog :deep(.modal-heading h2) {
  font-size: 16px;
  font-weight: 600;
}
.account-dialog :deep(.modal-heading button) {
  min-height: 30px;
  padding: 4px 10px;
  color: var(--muted);
}
.account-body {
  padding: 22px 26px 26px;
  overflow: auto;
  min-height: 0;
}
.account-summary {
  display: flex;
  align-items: center;
  gap: 13px;
  margin-bottom: 24px;
}
.account-avatar {
  display: grid;
  place-items: center;
  width: 44px;
  height: 44px;
  flex-shrink: 0;
  border: 1px solid #cde7e3;
  border-radius: 12px;
  background: #edf8f5;
  color: #168578;
}
.account-avatar .app-icon {
  width: 23px;
  height: 23px;
}
.account-summary-text {
  min-width: 0;
  flex: 1;
}
.account-summary h3 {
  font-size: 16px;
  margin: 0 0 3px;
  overflow-wrap: anywhere;
}
.account-summary p {
  font-size: 12px;
  color: var(--muted);
  margin: 0;
}
.account-status {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  flex-shrink: 0;
  background: #f1f5f9;
  color: #64748b;
  padding: 4px 9px;
  border-radius: 5px;
  font-size: 11px;
}
.account-status.success {
  background: #ecf8f1;
  color: #198252;
}
.account-status i {
  width: 5px;
  height: 5px;
  border-radius: 50%;
  background: currentColor;
}
.account-navigation {
  display: flex;
  align-items: center;
  justify-content: space-between;
  border-bottom: 1px solid var(--line);
  margin-bottom: 22px;
}
.account-tabs {
  display: flex;
  gap: 24px;
}
.account-dialog .account-tabs button {
  background: transparent;
  padding: 11px 0;
  border: 0;
  border-bottom: 2px solid transparent;
  border-radius: 0;
  color: var(--muted);
  font-size: 13px;
}
.account-dialog .account-tabs button.selected {
  color: #18796d;
  border-bottom-color: #168578;
  font-weight: 600;
}
.account-dialog .account-refresh {
  min-height: 28px;
  padding: 3px 9px;
  font-size: 11px;
}
.account-fields {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 18px 22px;
}
.account-fields label,
.account-fixed-field,
.account-upload > label {
  display: flex;
  flex-direction: column;
  gap: 7px;
  min-width: 0;
  font-size: 12px;
  color: #475569;
}
.account-fields input,
.account-fields select,
.account-fields textarea {
  width: 100%;
  min-width: 0;
  margin: 0;
  font-size: 13px;
}
.account-fields input,
.account-fields select {
  height: 38px;
}
.account-fields textarea {
  resize: vertical;
  min-height: 88px;
}
.account-fixed-field > div {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  min-height: 38px;
  padding: 7px 11px;
  border: 1px solid #e2e8f0;
  border-radius: 6px;
  background: #f8fafc;
}
.account-fixed-field a {
  color: #18796d;
  font-size: 13px;
  text-decoration: none;
}
.account-fixed-label {
  font-size: 10px;
  color: #94a3b8;
  white-space: nowrap;
}
.account-section-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 18px;
}
.account-section-heading h3 {
  margin: 0;
  font-size: 14px;
}
.account-section-heading p {
  margin: 5px 0 0;
  font-size: 12px;
  color: var(--muted);
}
.account-section-heading > button {
  flex-shrink: 0;
}
.account-credential {
  border: 1px solid var(--line);
  border-radius: 9px;
  padding: 18px;
}
.account-credential-heading {
  display: flex;
  justify-content: space-between;
  align-items: center;
  font-size: 13px;
  gap: 12px;
}
.account-facts {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 18px;
  margin: 18px 0;
}
.account-facts dt {
  color: var(--muted);
  font-size: 11px;
  margin-bottom: 4px;
}
.account-facts dd {
  font-size: 13px;
  margin: 0;
  overflow-wrap: anywhere;
}
.account-hint {
  margin: 12px 0;
  font-size: 12px;
  color: var(--muted);
}
.account-cookie-actions {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  padding-top: 14px;
  border-top: 1px solid #eef2f6;
}
.account-cookie-actions > a {
  margin-left: auto;
  font-size: 11px;
}
.account-more {
  position: relative;
  margin: 0;
  padding: 0;
  border: 0;
}
.account-more summary {
  cursor: pointer;
  color: var(--muted);
  font-size: 12px;
  padding: 8px;
}
.account-more .actions {
  position: absolute;
  z-index: 2;
  width: 145px;
  top: 30px;
  left: 0;
  padding: 8px;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: white;
  box-shadow: 0 6px 18px #17233615;
}
.account-more .actions button {
  width: 100%;
}
.account-manual {
  border: 1px solid var(--line);
  border-radius: 8px;
  margin-top: 16px;
  padding: 14px 16px;
}
.account-manual > summary {
  cursor: pointer;
  font-size: 12px;
  font-weight: 500;
}
.account-manual > summary span {
  font-weight: 400;
  color: var(--muted);
  margin-left: 10px;
  font-size: 11px;
}
.account-upload {
  display: grid;
  gap: 14px;
  margin-top: 18px;
}
.account-upload > .account-check {
  flex-direction: row;
  align-items: center;
}
.account-upload > button {
  justify-self: end;
}
.account-empty {
  display: flex;
  flex-direction: column;
  align-items: center;
  padding: 40px 18px;
  border: 1px dashed var(--line);
  border-radius: 9px;
  color: var(--muted);
}
.account-empty .app-icon {
  width: 26px;
  height: 26px;
  margin-bottom: 12px;
  color: #94a3b8;
}
.account-empty strong {
  color: #475569;
  font-size: 13px;
  font-weight: 500;
}
.account-empty p {
  font-size: 12px;
  margin: 6px 0 0;
  text-align: center;
}
.account-timeline {
  list-style: none;
  margin: 0;
  padding: 0 0 0 16px;
}
.account-timeline li {
  position: relative;
  padding: 0 0 22px 20px;
  border-left: 1px solid var(--line);
}
.account-timeline li::before {
  content: '';
  position: absolute;
  width: 7px;
  height: 7px;
  left: -4px;
  top: 6px;
  background: #a9c6c0;
  border-radius: 50%;
}
.account-event-heading {
  display: flex;
  justify-content: space-between;
  gap: 12px;
  font-size: 12px;
  font-weight: 500;
}
.account-event-heading time {
  font-size: 11px;
  color: var(--muted);
  font-weight: 400;
}
.account-timeline p {
  font-size: 12px;
  margin: 6px 0;
  overflow-wrap: anywhere;
}
.account-timeline small {
  display: block;
  color: var(--muted);
  font-size: 11px;
}
.account-footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 14px;
  padding: 15px 26px;
  border-top: 1px solid var(--line);
  background: #fafbfc;
  flex-shrink: 0;
}
.account-dialog .account-delete {
  padding: 4px 0;
  min-height: 30px;
  color: #b65252;
  background: transparent;
  border-color: transparent;
}
.account-footer-note,
.account-unsaved {
  font-size: 11px;
  color: var(--muted);
}
.account-unsaved {
  color: #ad7b2c;
}
.account-loading {
  color: var(--muted);
  font-size: 12px;
}
.account-dialog .primary {
  background: #178578;
  border-color: #178578;
}
.account-dialog .primary:not(:disabled):hover {
  background: #116b61;
  border-color: #116b61;
}
@media (max-width: 600px) {
  .account-dialog :deep(.modal-heading),
  .account-body,
  .account-footer {
    padding-left: 18px;
    padding-right: 18px;
  }
  .account-fields,
  .account-facts {
    grid-template-columns: 1fr;
  }
  .account-summary {
    display: grid;
    grid-template-columns: 44px minmax(0, 1fr);
  }
  .account-summary > .account-status {
    grid-column: 2;
    justify-self: start;
    margin: -5px 0 0;
  }
  .account-tabs {
    gap: 16px;
  }
  .account-tabs button {
    font-size: 12px;
  }
  .account-section-heading {
    align-items: flex-start;
    flex-wrap: wrap;
  }
  .account-footer-note,
  .account-unsaved {
    display: none;
  }
  .account-footer > .actions {
    margin-left: auto;
  }
  .account-event-heading {
    flex-direction: column;
    gap: 2px;
  }
  .account-cookie-actions > a {
    flex-basis: 100%;
    margin: 6px 0 0;
  }
}
</style>
