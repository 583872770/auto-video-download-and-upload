<script setup lang="ts">
import { computed, onMounted, onUnmounted, reactive, ref } from 'vue';
import { RouterLink } from 'vue-router';
import { api } from '../../api';
import WorkspaceDialog from '../../components/WorkspaceDialog.vue';
import { useInstances } from '../../stores/instances';
import { useOperation } from '../../composables/operation';
import { useResource } from '../../composables/resource';
import { useLeaveGuard } from '../../composables/leaveGuard';
import AccountLogin from './AccountLogin.vue';
import SessionProgress from './SessionProgress.vue';
import {
  base,
  authLabels,
  downloadLabels,
  at,
  type Account,
  type Cookie,
  type CookieEvent,
  type Node,
} from './contracts';
const props = defineProps<{ id?: string; nodes: Node[]; admin: boolean; blocked?: boolean }>(),
  emit = defineEmits<{ close: []; saved: [] }>();
const instances = useInstances(),
  op = useOperation(),
  item = ref<Account>(),
  baseline = ref(''),
  secret = ref(''),
  secretField = ref(''),
  tab = ref('profile'),
  login = ref(false),
  upload = ref<File>(),
  confirmed = ref(false);
const form = reactive({
  email: '',
  password: '',
  extra: '',
  code_url: '',
  note: '',
  instance_id: '',
  change_extra: false,
  change_code_url: false,
});
const dirty = computed(
  () => (!!baseline.value && JSON.stringify(form) !== baseline.value) || !!upload.value,
);
const events = useResource<CookieEvent[]>(),
  kind = ref('');
const safeUrl = computed(() => {
  if (secretField.value !== 'code_url') return undefined;
  try {
    const u = new URL(secret.value);
    return ['http:', 'https:'].includes(u.protocol) ? u.href : undefined;
  } catch {
    return undefined;
  }
});
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
    code_url: '',
    change_extra: false,
    change_code_url: false,
  });
  baseline.value = JSON.stringify(form);
  secret.value = '';
  secretField.value = '';
  upload.value = undefined;
  confirmed.value = false;
}
async function read() {
  const id = item.value?.id || props.id;
  if (id) item.value = (await api.request<{ item: Account }>(base + '/' + id)).item;
  fill();
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
      ...(!item.value || form.change_extra ? { extra: form.extra } : {}),
      ...(!item.value || form.change_code_url ? { code_url: form.code_url } : {}),
      ...(item.value ? { version: item.value.version } : {}),
    };
    const value = await api.request<{ id?: string; message: string }>(
      base + (item.value ? '/' + item.value.id : ''),
      { method: item.value ? 'PATCH' : 'POST', body },
    );
    const id = item.value?.id || value.id;
    item.value = (await api.request<{ item: Account }>(base + '/' + id)).item;
    fill();
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
async function reveal(field: string) {
  await op.run(async () => {
    secret.value = '';
    secretField.value = field;
    secret.value = (
      await api.request<{ value: string }>(base + '/' + item.value!.id + '/secrets', {
        method: 'POST',
        body: { field },
      })
    ).value;
  });
}
async function copy() {
  await op.run(async () => {
    try {
      await navigator.clipboard.writeText(secret.value);
      op.message.value = '已复制。';
    } catch {
      throw new Error('复制不可用，请手动选中资料复制。');
    }
  });
}
function requireOwner(cookie: Cookie) {
  if (cookie.instance_id !== instances.selectedId && instances.selectedId)
    throw new Error('请先切换到 Cookie 所属实例。');
  if (dirty.value || props.blocked) throw new Error('请先保存账号、账号池和节奏修改。');
  return {
    id: cookie.instance_id,
    epoch: 0,
    isCurrent: () =>
      alive && (!instances.selectedId || instances.selectedId === cookie.instance_id),
  };
}
let alive = true;
onUnmounted(() => {
  alive = false;
  secret.value = '';
  Object.assign(form, { password: '', extra: '', code_url: '' });
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
async function showEvents() {
  tab.value = 'schedule';
  await history();
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
    :open="true"
    :title="item?.email || '新增账号'"
    :busy="op.busy.value"
    :before-close="beforeClose"
    @close="emit('close')"
  >
    <p v-if="op.error.value" class="notice error" role="alert">{{ op.error.value }} 输入已保留。</p>
    <p v-if="op.message.value" class="notice" role="status">{{ op.message.value }}</p>
    <div class="toolbar">
      <button
        v-for="[key, label] in [
          ['profile', '资料'],
          ['cookies', 'Cookie 与登录'],
          ['schedule', '调度与记录'],
        ]"
        :key="key"
        :disabled="!item && key !== 'profile'"
        :aria-pressed="tab === key"
        @click="key === 'schedule' ? showEvents() : (tab = key!)"
      >
        {{ label }}</button
      ><button v-if="item" :disabled="op.busy.value" @click="reload">
        {{ dirty ? '放弃修改并重新读取' : '重新读取' }}
      </button>
    </div>
    <form v-if="tab === 'profile'" @submit.prevent="save">
      <fieldset :disabled="!admin || op.busy.value">
        <div class="form-grid">
          <label
            >邮箱账号<input
              v-model="form.email"
              type="email"
              required
              maxlength="254"
              :disabled="!!item?.cookie_count"
              autocomplete="off" /></label
          ><label v-if="item"
            >绑定实例<select v-model="form.instance_id" :disabled="!!item.cookie_count">
              <option value="">登录时绑定</option>
              <option v-for="node in nodes" :key="node.id" :value="node.id">
                {{ node.name }} · {{ node.id }}
              </option>
            </select></label
          ><label
            >密码<input
              v-model="form.password"
              type="password"
              :required="!item"
              maxlength="16384"
              autocomplete="new-password"
              placeholder="编辑留空保留密码" /></label
          ><label
            >附加信息<textarea v-model="form.extra" maxlength="16384" autocomplete="off"></textarea
            ><span v-if="item"
              ><input v-model="form.change_extra" type="checkbox" />修改附加信息（允许清空）</span
            ></label
          ><label
            >接码网址<input
              v-model="form.code_url"
              type="url"
              maxlength="4096"
              autocomplete="off"
              placeholder="https://"
            /><span v-if="item"
              ><input
                v-model="form.change_code_url"
                type="checkbox"
              />修改接码网址（允许清空）</span
            ></label
          ><label>备注<textarea v-model="form.note" maxlength="4000" rows="2"></textarea></label>
        </div>
        <div class="actions">
          <button class="primary">保存账号</button
          ><button v-if="item" type="button" @click="remove">删除账号资料</button>
        </div>
      </fieldset>
    </form>
    <template v-if="tab === 'profile' && item && admin"
      ><div class="actions">
        <button
          v-for="[key, label] in [
            ['password', '密码'],
            ['extra', '附加信息'],
            ['code_url', '接码网址'],
          ]"
          :key="key"
          :disabled="op.busy.value"
          @click="reveal(key!)"
        >
          查看{{ label }}
        </button>
      </div>
      <div v-if="secretField" class="notice">
        <label>按需读取的资料<input :value="secret" readonly autocomplete="off" /></label>
        <div class="actions">
          <button @click="copy">复制</button
          ><a v-if="safeUrl" :href="safeUrl" target="_blank" rel="noopener noreferrer"
            >打开接码网址 ↗</a
          ><button
            @click="
              secret = '';
              secretField = '';
            "
          >
            隐藏
          </button>
        </div>
      </div></template
    >
    <section v-if="tab === 'cookies'">
      <p>每个账号仅保留一份当前 Cookie；重新登录成功后替换，失败或取消保留原凭据。</p>
      <article v-for="cookie in item?.cookies" :key="cookie.id" class="ledger-row">
        <h3>{{ cookie.name }}</h3>
        <p>
          {{ cookie.instance_name }} · {{ cookie.instance_id }} ·
          {{ cookie.online ? '在线' : '离线' }} ·
          {{ cookie.pooled ? '账号池成员' : '未加入账号池' }}
        </p>
        <p>
          登录：{{ authLabels[cookie.auth.state] || cookie.auth.state }} ·
          {{ at(cookie.auth.checked_at) }} · {{ cookie.auth.connection }}
        </p>
        <p>{{ cookie.auth.message }}</p>
        <p>
          上次验证通过 {{ at(cookie.auth.last_valid_at) }} · 下载：{{
            downloadLabels[cookie.download_state]
          }}
        </p>
        <div v-if="admin" class="actions">
          <button
            :disabled="
              op.busy.value || !cookie.online || cookie.download_state === 'cooldown' || dirty
            "
            @click="cookieAction(cookie, 'check')"
          >
            检测登录</button
          ><button
            v-for="[action, label] in [
              ['export', '导出 Cookie'],
              ['select', '单独使用'],
              ['delete', '删除 Cookie'],
            ]"
            :key="action"
            :disabled="
              op.busy.value ||
              dirty ||
              blocked ||
              (!!instances.selectedId && instances.selectedId !== cookie.instance_id)
            "
            @click="cookieAction(cookie, action!)"
          >
            {{ label }}
          </button>
        </div>
        <RouterLink
          :to="{ path: '/accounts', query: { instance_id: cookie.instance_id, tab: 'pool' } }"
          >所属实例账号池与节奏</RouterLink
        >
      </article>
      <p v-if="!item?.cookies.length" class="empty">尚未保存 Cookie。</p>
      <button
        v-if="admin && item"
        :disabled="op.busy.value || dirty || (!!item.instance_id && !item.online)"
        @click="login = true"
      >
        登录 / 重新登录
      </button>
      <form v-if="admin && item?.instance_id" class="card" @submit.prevent="uploadCookie">
        <h3>手工更新 Cookie</h3>
        <p>绑定实例：{{ item.instance_name }} · {{ item.instance_id }}</p>
        <label
          >Netscape Cookie TXT<input
            :key="item.version + '-' + !!upload"
            type="file"
            accept=".txt,text/plain"
            :disabled="op.busy.value"
            @change="upload = ($event.target as HTMLInputElement).files?.[0]" /></label
        ><label
          ><input v-model="confirmed" type="checkbox" :disabled="op.busy.value" />确认 Cookie
          属于此账号，并替换原凭据</label
        ><button :disabled="op.busy.value || !upload || !confirmed">替换并保存</button>
      </form>
    </section>
    <section v-if="tab === 'schedule'">
      <div v-for="cookie in item?.cookies" :key="cookie.id">
        <h3>{{ cookie.name }}</h3>
        <SessionProgress :session="cookie.session" :online="cookie.online" />
      </div>
      <div class="toolbar">
        <label
          >记录类型<select v-model="kind">
            <option value="">全部</option>
            <option value="interval_change">间隔调整</option>
            <option value="recovery">恢复阶段</option>
            <option value="failure">异常与退避</option>
            <option value="auth_check">登录检测</option>
          </select></label
        ><button
          :disabled="events.loading.value || op.busy.value || dirty"
          @click="
            op.run(async () => {
              await read();
              await history();
            })
          "
        >
          刷新状态与记录
        </button>
      </div>
      <p v-if="events.error.value" class="form-error" role="alert">{{ events.error.value }}</p>
      <article
        v-for="(event, index) in events.data.value?.filter((e) => !kind || e.event_type === kind)"
        :key="index"
        class="ledger-row"
      >
        <small
          >{{ at(event.occurred_at) }} · {{ event.profile_name }} · {{ event.event_type }}</small
        >
        <p>{{ event.message || event.kind }}</p>
        <p v-if="event.previous_interval != null">
          记录间隔 {{ event.previous_interval }} → {{ event.new_interval }} 秒
        </p>
        <p v-if="event.cooldown_until">冷却截止 {{ at(event.cooldown_until) }}</p>
      </article>
      <p v-if="events.data.value && !events.data.value.length" class="empty">暂无事件记录。</p>
    </section>
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
  </WorkspaceDialog>
</template>
