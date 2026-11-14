<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue';
import { api } from '../../api';
import { useInstances } from '../../stores/instances';
import { useOperation } from '../../composables/operation';
import { useResource } from '../../composables/resource';
import { usePolling } from '../../composables/polling';
import { useLeaveGuard } from '../../composables/leaveGuard';
import { constraints, type ConsoleConfig } from '../../generated/config';
import { authLabels, base, type Cookie, type CookieEvent } from './contracts';
import SessionProgress from './SessionProgress.vue';
const props = defineProps<{ admin: boolean }>(),
  emit = defineEmits<{ pending: [value: boolean]; saved: [] }>();
const instances = useInstances(),
  op = useOperation(),
  resource = useResource<{
    items: Cookie[];
    pool: { halted?: boolean; cooldown_until?: number } | null;
    custom_active: boolean;
  }>();
const config = ref<ConsoleConfig>(),
  ids = ref<string[]>([]),
  enabled = ref(false),
  membersBaseline = ref(''),
  pacingBaseline = ref('');
const pacing = reactive<Record<string, string | number | boolean>>({}),
  history = useResource<CookieEvent[]>(),
  selected = ref('');
const fields = [
  {
    key: 'youtube_pacing_mode',
    label: 'YouTube 请求节奏',
    options: [
      ['manual', '固定间隔'],
      ['adaptive', '渐进调速与累计预算'],
    ],
    help: '非账号池模式下使用；渐进模式从至少 60 秒起步，稳定交付后逐步提速。',
  },
  {
    key: 'youtube_cookie_usage',
    label: 'Cookie 使用策略',
    options: [
      ['auto', '按需使用（推荐）'],
      ['always', '始终使用'],
      ['never', '仅匿名'],
    ],
    help: '按需先匿名，仅明确要求登录时使用当前 Cookie 重试。账号池始终使用池内会话。',
  },
  {
    key: 'youtube_cookie_pool_start_interval',
    label: '账号池共用启动间隔（秒）',
    help: '全部 Cookie 共用，0 关闭此间隔。新任务使用新值，已有等待窗口保留到期。',
  },
  {
    key: 'youtube_start_interval',
    label: '两次任务启动最小间隔（秒）',
    help: '账号池逐账号计算。预热、冷却、恢复保护可能延长实际间隔，降低配置不会立即解除保护。',
  },
  {
    key: 'youtube_task_rest_interval',
    label: '任务结束后休息（秒）',
    help: '成功及已发起请求的失败均生效，0 关闭；与启动间隔取更晚时间，不直接相加。',
  },
  {
    key: 'youtube_cookie_session_limit',
    label: '每 Cookie 会话任务上限',
    help: '预热和恢复期仍限制为 1 个，实际进程另受内存、数据库及进程上限约束。',
  },
  {
    key: 'youtube_hourly_start_limit',
    label: '滚动一小时启动预算',
    help: '0 不限制；账号池固定与渐进模式均生效，视频与重试共用额度。',
  },
  {
    key: 'youtube_daily_start_limit',
    label: '滚动二十四小时启动预算',
    help: '0 不限制；保存节奏、重启或重新导入不会清空账号池预算记录。',
  },
  {
    key: 'youtube_cookie_fallback',
    label: 'Cookie 不可用时匿名重试',
    options: [
      [false, '保留原始错误（推荐）'],
      [true, '同一出口匿名重试一次'],
    ],
    help: '验证失败只暂停可归属的账号，匿名重试可能再次触发验证。',
  },
  {
    key: 'youtube_sleep_preset',
    label: 'yt-dlp sleep 预设',
    options: [
      [true, '启用（推荐）'],
      [false, '关闭'],
    ],
    help: '解析间隔 0.75 秒，视频、音频和字幕文件开始前统一等待 10～20 秒，不限制已开始的字节传输。',
  },
  {
    key: 'youtube_throttle_cooldown',
    label: 'HTTP 429 最短冷却（秒）',
    help: '源站 Retry-After 更长时采用更长等待，到期仍需单路恢复观察。',
  },
];
const membersDirty = computed(
  () =>
    !!membersBaseline.value &&
    JSON.stringify({ enabled: enabled.value, ids: ids.value }) !== membersBaseline.value,
);
const pacingDirty = computed(
  () => !!pacingBaseline.value && JSON.stringify(pacing) !== pacingBaseline.value,
);
const dirty = computed(() => membersDirty.value || pacingDirty.value);
watch([dirty, op.busy], () => emit('pending', dirty.value || op.busy.value), { immediate: true });
useLeaveGuard(() => ({ dirty: dirty.value, busy: op.busy.value }));
function range(key: string) {
  return constraints[key as keyof typeof constraints];
}
function update(key: string, event: Event) {
  const raw = (event.target as HTMLSelectElement).value,
    field = fields.find((f) => f.key === key);
  const option = field?.options?.find(([value]) => String(value) === raw);
  pacing[key] = option ? option[0]! : Number(raw);
}
async function refresh(signal?: AbortSignal) {
  await resource.load(async (s) => {
    const value = await api.request<{
      items: Array<Omit<Cookie, 'auth'> & { auth: Cookie['auth'] | null }>;
      pool: { halted?: boolean; cooldown_until?: number } | null;
      custom_active: boolean;
    }>('/api/youtube/cookies', { signal: s, scope: instances.scope() });
    return {
      ...value,
      items: value.items.map((cookie) => ({
        ...cookie,
        auth: cookie.auth || { state: 'unchecked' },
      })),
    };
  }, signal);
  if (resource.data.value && !membersDirty.value) {
    ids.value = resource.data.value.items.filter((c) => c.pooled).map((c) => c.id);
    enabled.value = !!resource.data.value.pool;
    membersBaseline.value = JSON.stringify({ enabled: enabled.value, ids: ids.value });
  }
}
async function load() {
  const scope = instances.scope();
  const value = await api.request<{ config: ConsoleConfig }>('/api/config', { scope });
  config.value = value.config;
  if (!pacingDirty.value) {
    for (const field of fields)
      pacing[field.key] = value.config[field.key as keyof ConsoleConfig] as
        | string
        | number
        | boolean;
    pacingBaseline.value = JSON.stringify(pacing);
  }
  await refresh();
}
async function discard() {
  if (dirty.value && !window.confirm('放弃账号池和节奏的未保存修改？')) return;
  membersBaseline.value = '';
  pacingBaseline.value = '';
  await op.run(load);
}
async function save(members: boolean) {
  await op.run(async () => {
    if (!config.value) throw new Error('请先重新读取配置。');
    if (members && enabled.value && ids.value.length < 2)
      throw new Error('启用账号池至少需要两份 Cookie。');
    if (
      members &&
      enabled.value &&
      (config.value.download_methods || [config.value.download_method]).some((p) => p !== 'yt_dlp')
    )
      throw new Error('账号池仅支持单独使用 yt-dlp，请先保存下载策略。');
    const body: Record<string, unknown> = members
      ? { youtube_cookie_pool: enabled.value ? [...ids.value] : [] }
      : Object.fromEntries(
          Object.entries(pacing).filter(
            ([key, value]) => JSON.parse(pacingBaseline.value)[key] !== value,
          ),
        );
    const scope = instances.scope();
    await api.request('/api/config', { method: 'PUT', scope, body });
    const value = await api.request<{ config: ConsoleConfig }>('/api/config', { scope });
    if (
      Object.entries(body).some(
        ([key, expected]) =>
          JSON.stringify(value.config[key as keyof ConsoleConfig]) !== JSON.stringify(expected),
      )
    )
      throw new Error('服务器读回与提交值不同，草稿已保留。');
    config.value = value.config;
    if (members) {
      membersBaseline.value = '';
      await refresh();
    } else pacingBaseline.value = JSON.stringify(pacing);
    op.message.value = members
      ? '账号池已保存，保留冷却与运行记录。'
      : '节奏已保存，用于后续任务。';
    emit('saved');
  });
}
async function tool(cookie: Cookie, action: string) {
  await op.run(async () => {
    if (dirty.value) throw new Error('请先保存或放弃账号池和节奏修改。');
    if (action === 'delete' && !window.confirm(`删除 Cookie「${cookie.name}」？`)) return;
    if (
      action === 'select' &&
      !window.confirm(`单独使用「${cookie.name}」将关闭当前实例账号池，继续？`)
    )
      return;
    const scope = instances.scope();
    if (action === 'export') {
      const blob = await api.request<Blob>('/api/youtube/cookies/export', {
        method: 'POST',
        scope,
        body: { id: cookie.id },
        responseType: 'blob',
      });
      const url = URL.createObjectURL(blob),
        a = document.createElement('a');
      a.href = url;
      a.download = 'youtube-cookie-' + cookie.id + '.txt';
      a.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } else {
      await api.request('/api/youtube/cookies/' + action, {
        method: 'POST',
        scope,
        body: { id: cookie.id },
      });
      await load();
      emit('saved');
    }
  });
}
async function recover() {
  await op.run(async () => {
    const result = await api.request<{ message: string }>('/api/youtube/cookies/pool/recover', {
      method: 'POST',
      scope: instances.scope(),
      body: {},
    });
    await refresh();
    op.message.value = result.message;
  });
}
async function events(id: string) {
  selected.value = id;
  await history.load(
    async (signal) =>
      (
        await api.request<{ events: CookieEvent[] }>(
          `/api/youtube/cookies/${encodeURIComponent(id)}/timeline`,
          { signal, scope: instances.scope() },
        )
      ).events,
  );
}
usePolling(
  computed(() => !op.busy.value),
  async (signal) => {
    try {
      if (!config.value) await load();
      else await refresh(signal);
    } catch (error) {
      if (!signal.aborted) op.error.value = error instanceof Error ? error.message : String(error);
      throw error;
    }
  },
  10000,
);
</script>
<template>
  <section class="card">
    <div class="page-heading">
      <div>
        <h2>{{ instances.name }} · 账号池与节奏</h2>
        <p>仅影响当前实例 {{ instances.selectedId }}。成员与节奏独立保存。</p>
      </div>
      <button :disabled="op.busy.value" @click="discard">
        {{ dirty ? '放弃修改并重新读取' : '重新读取' }}
      </button>
    </div>
    <p v-if="op.error.value || resource.error.value" class="notice error" role="alert">
      {{ op.error.value || resource.error.value }}
    </p>
    <p v-if="op.message.value" class="notice" role="status">{{ op.message.value }}</p>
    <p v-if="!config" role="status">正在读取实例配置…</p>
    <template v-else
      ><fieldset :disabled="!admin || op.busy.value">
        <legend>账号池成员</legend>
        <label
          ><input v-model="enabled" type="checkbox" />启用账号池（至少 2 份 Cookie，仅
          yt-dlp）</label
        >
        <p>
          已选 {{ ids.length }} 份 Cookie{{ membersDirty ? '，尚未保存' : '' }}。登录成功、固定 IP
          与更多 IPv6 不代表没有源站配额限制。
        </p>
        <p v-if="resource.data.value?.custom_active" class="notice">
          当前使用自定义路径 Cookie，尚未列入保存的 Cookie 清单。
        </p>
        <div class="table-scroll">
          <table>
            <thead>
              <tr>
                <th>入池</th>
                <th>Cookie</th>
                <th>登录状态</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="cookie in resource.data.value?.items" :key="cookie.id">
                <td>
                  <input
                    v-model="ids"
                    type="checkbox"
                    :value="cookie.id"
                    :aria-label="'加入账号池 ' + cookie.name"
                  />
                </td>
                <td>{{ cookie.name }}{{ cookie.active ? '（当前选用）' : '' }}</td>
                <td>{{ authLabels[cookie.auth.state] || cookie.auth.state }}</td>
                <td>
                  <button
                    v-for="[action, label] in [
                      ['check', '检测'],
                      ['export', '导出'],
                      ['select', '单独使用'],
                      ['delete', '删除'],
                    ]"
                    :key="action"
                    type="button"
                    :disabled="dirty || op.busy.value"
                    @click="tool(cookie, action!)"
                  >
                    {{ label }}</button
                  ><button type="button" @click="events(cookie.id)">记录</button>
                </td>
              </tr>
              <tr v-if="resource.data.value && !resource.data.value.items.length">
                <td colspan="4" class="empty">暂无 Cookie，请从账号详情登录或上传。</td>
              </tr>
            </tbody>
          </table>
        </div>
        <button :disabled="!membersDirty" @click="save(true)">保存账号池</button
        ><button v-if="resource.data.value?.pool?.halted" :disabled="dirty" @click="recover">
          检测账号池并解除拦截
        </button>
      </fieldset>
      <details v-for="cookie in resource.data.value?.items" :key="cookie.id">
        <summary>{{ cookie.name }} · 调度与恢复进度</summary>
        <SessionProgress :session="cookie.session" :online="!resource.error.value" />
      </details>
      <section v-if="selected">
        <h3>Cookie 记录</h3>
        <p v-if="history.error.value" class="form-error">{{ history.error.value }}</p>
        <article v-for="(event, index) in history.data.value" :key="index" class="ledger-row">
          <small
            >{{ new Date(event.occurred_at * 1000).toLocaleString('zh-CN') }} ·
            {{ event.event_type }}</small
          >
          <p>{{ event.message || event.kind }}</p>
          <p v-if="event.previous_interval != null">
            间隔 {{ event.previous_interval }} → {{ event.new_interval }} 秒
          </p>
        </article>
        <p v-if="history.data.value && !history.data.value.length">暂无记录。</p>
        <button @click="events(selected)">刷新记录</button
        ><button
          @click="
            selected = '';
            history.clear();
          "
        >
          关闭记录
        </button>
      </section>
      <form @submit.prevent="save(false)">
        <fieldset :disabled="!admin || op.busy.value">
          <legend>任务节奏与启动预算</legend>
          <div class="form-grid">
            <label v-for="field in fields" :key="field.key"
              >{{ field.label
              }}<select
                v-if="field.options"
                :value="String(pacing[field.key])"
                @change="update(field.key, $event)"
              >
                <option
                  v-for="[value, label] in field.options"
                  :key="String(value)"
                  :value="String(value)"
                >
                  {{ label }}
                </option></select
              ><input
                v-else
                :value="pacing[field.key]"
                type="number"
                :min="range(field.key)?.[0]"
                :max="range(field.key)?.[1]"
                required
                @input="update(field.key, $event)"
              /><small>{{ field.help }}</small></label
            >
          </div>
          <p class="muted">
            可归属的限流只冷却该账号；一天内重复限流按 1 / 2 / 4 / 8 小时递增。到期自动单路、至少
            300 秒恢复，需同时满足有效成功与观察时间；普通网络错误不重置账号恢复进度。
          </p>
          <button :disabled="!pacingDirty">保存节奏</button>
        </fieldset>
      </form></template
    >
  </section>
</template>
