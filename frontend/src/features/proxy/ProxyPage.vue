<script setup lang="ts">
import { computed, reactive, ref } from 'vue';
import { api } from '../../api';
import { bootstrap } from '../../bootstrap';
import InstancePicker from '../../components/InstancePicker.vue';
import { useInstances } from '../../stores/instances';
import { useResource } from '../../composables/resource';
import { useOperation } from '../../composables/operation';
import { usePolling } from '../../composables/polling';
import { useLeaveGuard } from '../../composables/leaveGuard';
interface Rotation {
  enabled: boolean;
  nodes: string[];
  every: number;
  count_failures: boolean;
  completed?: number;
  remaining?: number;
  next_node?: string;
  workers?: Array<{ slot: number; completed: number; next_node: string }>;
}
interface Clash {
  installed: boolean;
  running: boolean;
  selected: string;
  proxy_url: string;
  mode: string;
  error: string;
  locked: boolean;
  connection_edit_locked: boolean;
  connection_apply_locked: boolean;
  connection_apply_reason: string;
  subscription_locked: boolean;
  subscription_lock_reason: string;
  rotation_locked: boolean;
  rotation_lock_reason: string;
  subscriptions: Array<{
    id: string;
    name: string;
    host: string;
    node_count: number;
    updated_at: string;
  }>;
  active_subscription_id: string;
  nodes: Array<{ name: string; type: string; delay: number | null; cooling_until: number }>;
  notices: string[];
  testing: { running: boolean; total?: number; done?: number };
  rotation: Rotation;
}
const instances = useInstances(),
  resource = useResource<Clash>(),
  op = useOperation();
const editable = bootstrap().role === 'admin';
const connection = reactive({ proxy_mode: 'direct', proxy: '', youtube_source_address: '' }),
  connectionBaseline = ref('');
const rotation = reactive<Rotation>({ enabled: false, nodes: [], every: 1, count_failures: true }),
  rotationBaseline = ref('');
const subscription = reactive({ id: '', name: '', url: '' }),
  subscriptionBaseline = ref(JSON.stringify(subscription));
const query = ref(''),
  sort = ref('original');
const connectionDirty = computed(
  () => JSON.stringify(connection) !== connectionBaseline.value && !!connectionBaseline.value,
);
const rotationDirty = computed(
  () => JSON.stringify(rotation) !== rotationBaseline.value && !!rotationBaseline.value,
);
const subscriptionDirty = computed(
  () => JSON.stringify(subscription) !== subscriptionBaseline.value,
);
const dirty = computed(
  () => connectionDirty.value || rotationDirty.value || subscriptionDirty.value,
);
useLeaveGuard(() => ({ dirty: dirty.value, busy: op.busy.value }));
const nodes = computed(() => {
  const values = (resource.data.value?.nodes || []).filter((n) =>
    (n.name + ' ' + n.type).toLowerCase().includes(query.value.toLowerCase()),
  );
  return sort.value === 'delay'
    ? values.sort(
        (a, b) =>
          (a.delay && a.delay > 0 ? a.delay : Infinity) -
          (b.delay && b.delay > 0 ? b.delay : Infinity),
      )
    : sort.value === 'name'
      ? values.sort((a, b) => a.name.localeCompare(b.name))
      : values;
});
function choose() {
  const item = resource.data.value?.subscriptions.find((s) => s.id === subscription.id);
  subscription.name = item?.name || '';
  subscription.url = '';
  subscriptionBaseline.value = JSON.stringify(subscription);
}
async function refresh(signal?: AbortSignal) {
  const scope = instances.scope();
  await resource.load((s) => api.request('/api/clash', { scope, signal: s }), signal);
  if (resource.data.value && !rotationDirty.value) {
    const r = resource.data.value.rotation;
    Object.assign(rotation, {
      enabled: r.enabled,
      nodes: [...r.nodes],
      every: r.every,
      count_failures: r.count_failures,
    });
    rotationBaseline.value = JSON.stringify(rotation);
  }
}
async function loadConnection() {
  const response = await api.request<{ config: typeof connection }>('/api/config', {
    scope: instances.scope(),
  });
  Object.assign(connection, {
    proxy_mode: response.config.proxy_mode,
    proxy: response.config.proxy || '',
    youtube_source_address: response.config.youtube_source_address || '',
  });
  connectionBaseline.value = JSON.stringify(connection);
}
async function changed() {
  resource.clear();
  connectionBaseline.value = '';
  rotationBaseline.value = '';
  Object.assign(subscription, { id: '', name: '', url: '' });
  subscriptionBaseline.value = JSON.stringify(subscription);
  await op.run(async () => {
    await Promise.all([refresh(), loadConnection()]);
  });
}
async function action(path: string, body: unknown = {}) {
  await op.run(async () => {
    const value = await api.request<{ message?: string; ms?: number; node?: string }>(path, {
      method: 'POST',
      body,
      scope: instances.scope(),
    });
    await refresh();
    if (resource.error.value) throw new Error('操作已提交，读取状态失败，请刷新核对。');
    op.message.value =
      value.message ||
      (path.endsWith('/test')
        ? `YouTube 连接成功 · ${value.ms} ms · ${value.node}`
        : '操作已提交，状态已刷新。');
  });
}
async function saveConnection() {
  await op.run(async () => {
    const expected = { ...connection };
    await api.request('/api/config', { method: 'PUT', body: expected, scope: instances.scope() });
    const saved = await api.request<{ config: typeof connection }>('/api/config', {
      scope: instances.scope(),
    });
    if (
      Object.entries(expected).some(
        ([key, value]) =>
          String(saved.config[key as keyof typeof connection] ?? '') !== String(value),
      )
    )
      throw new Error('连接配置尚未读回确认，草稿已保留。');
    connectionBaseline.value = JSON.stringify(connection);
    await refresh();
    op.message.value = '连接配置已保存，后续任务使用新配置。';
  });
}
async function saveSubscription(actionName: string) {
  await op.run(async () => {
    if (actionName === 'delete' && !window.confirm(`删除订阅「${subscription.name}」？`)) return;
    const value = await api.request<{ message: string; saved_subscription_id?: string }>(
      '/api/clash/subscription',
      {
        method: 'POST',
        scope: instances.scope(),
        body:
          actionName === 'save'
            ? { ...subscription, action: actionName }
            : { id: subscription.id, action: actionName },
      },
    );
    subscription.url = '';
    await refresh();
    if (resource.error.value) throw new Error('订阅操作已提交，状态读取失败，请刷新核对。');
    if (actionName === 'delete') subscription.id = '';
    else if (value.saved_subscription_id) subscription.id = value.saved_subscription_id;
    choose();
    op.message.value = value.message;
  });
}
async function saveRotation() {
  await op.run(async () => {
    const expected = JSON.stringify(rotation);
    await api.request('/api/clash/rotation', {
      method: 'POST',
      body: { ...rotation },
      scope: instances.scope(),
    });
    await refresh();
    const r = resource.data.value?.rotation;
    if (
      resource.error.value ||
      !r ||
      JSON.stringify({
        enabled: r.enabled,
        nodes: r.nodes,
        every: r.every,
        count_failures: r.count_failures,
      }) !== expected
    )
      throw new Error('轮换配置尚未读回确认，草稿已保留。');
    rotationBaseline.value = expected;
    op.message.value = '自动轮换配置已保存。';
  });
}
usePolling(
  computed(() => !!connectionBaseline.value && !op.busy.value),
  refresh,
  5000,
);
</script>
<template>
  <main class="content">
    <div class="page-heading">
      <div>
        <span class="eyebrow">NETWORK & PROXY</span>
        <h1>网络与代理</h1>
        <p>连接、订阅和节点配置只影响所选实例。</p>
      </div>
      <button :disabled="!editable || op.busy.value" @click="refresh()">刷新状态</button>
    </div>
    <InstancePicker :disabled="!editable || dirty || op.busy.value" @change="changed" />
    <p v-if="op.error.value || resource.error.value" class="notice error" role="alert">
      {{ op.error.value || resource.error.value }}
    </p>
    <p v-if="op.message.value" class="notice" role="status">{{ op.message.value }}</p>
    <template v-if="resource.data.value"
      ><p v-if="resource.data.value.error" class="notice error">{{ resource.data.value.error }}</p>
      <p v-if="resource.data.value.locked" class="notice">
        {{
          resource.data.value.subscription_lock_reason ||
          '请停止下载并等待当前任务结束，再修改连接。'
        }}
      </p>
      <form class="card" @submit.prevent="saveConnection">
        <h2>下载连接</h2>
        <fieldset
          :disabled="!editable || op.busy.value || resource.data.value.connection_edit_locked"
        >
          <div class="form-grid">
            <label
              >连接方式<select v-model="connection.proxy_mode">
                <option value="direct">直连</option>
                <option value="manual">手动代理</option>
                <option value="clash">Clash 订阅代理</option>
              </select></label
            ><label
              >手动代理地址<input
                v-model="connection.proxy"
                placeholder="http://127.0.0.1:7890"
                autocomplete="off" /></label
            ><label
              >直连源地址<input
                v-model="connection.youtube_source_address"
                placeholder="留空自动选择；:: 表示仅 IPv6"
            /></label>
          </div>
          <p class="muted">
            直连源地址同时用于登录检测和 yt-dlp。:: 由系统选择 IPv6，Docker 需先具备 IPv6
            网络。已有传输继续使用原连接。
          </p>
          <button :disabled="!editable || !connectionDirty">保存连接配置</button>
        </fieldset>
        <p>
          内核：{{
            resource.data.value.running
              ? '运行中'
              : resource.data.value.installed
                ? '已安装，未运行'
                : '未安装'
          }}
          · 当前节点 {{ resource.data.value.selected || '未选择' }}
        </p>
        <div class="actions">
          <button
            type="button"
            :disabled="!editable || op.busy.value || resource.data.value.locked"
            @click="action('/api/clash/start')"
          >
            启动代理</button
          ><button
            type="button"
            :disabled="!editable || op.busy.value || resource.data.value.locked"
            @click="action('/api/clash/stop')"
          >
            停止代理</button
          ><button
            type="button"
            :disabled="!editable || op.busy.value"
            @click="action('/api/clash/test')"
          >
            测试 YouTube</button
          ><button
            type="button"
            :disabled="
              !editable || op.busy.value || dirty || resource.data.value.connection_apply_locked
            "
            @click="action('/api/downloads/connection')"
          >
            检查当前连接
          </button>
        </div>
        <p class="muted">{{ resource.data.value.connection_apply_reason }}</p>
      </form>
      <form class="card" @submit.prevent="saveSubscription('save')">
        <h2>订阅管理</h2>
        <fieldset :disabled="!editable || op.busy.value || resource.data.value.subscription_locked">
          <label
            >已保存订阅<select
              v-model="subscription.id"
              :disabled="!editable || subscriptionDirty"
              @change="choose"
            >
              <option value="">＋ 新增订阅</option>
              <option
                v-for="item in resource.data.value.subscriptions"
                :key="item.id"
                :value="item.id"
              >
                {{ item.name
                }}{{
                  item.id === resource.data.value.active_subscription_id ? '（当前使用）' : ''
                }}
                · {{ item.host }} · {{ item.node_count }} 节点
              </option>
            </select></label
          >
          <div class="form-grid">
            <label>订阅名称<input v-model="subscription.name" required maxlength="80" /></label
            ><label
              >订阅链接<input
                v-model="subscription.url"
                type="password"
                autocomplete="new-password"
                :required="!subscription.id"
                maxlength="8192"
                placeholder="已有订阅留空更新原链接"
            /></label>
          </div>
          <p class="muted">链接保存在当前实例，保存后不回显。首个订阅自动启用。</p>
          <div class="actions">
            <button>保存 / 更新订阅</button
            ><button
              type="button"
              :disabled="
                !editable ||
                !subscription.id ||
                subscriptionDirty ||
                subscription.id === resource.data.value.active_subscription_id
              "
              @click="saveSubscription('switch')"
            >
              切换到此订阅</button
            ><button
              type="button"
              :disabled="
                !editable ||
                !subscription.id ||
                subscriptionDirty ||
                subscription.id === resource.data.value.active_subscription_id
              "
              @click="saveSubscription('delete')"
            >
              删除订阅</button
            ><button v-if="subscriptionDirty" type="button" @click="choose">放弃订阅修改</button>
          </div>
        </fieldset>
        <p v-for="notice in resource.data.value.notices" :key="notice">{{ notice }}</p>
      </form>
      <form class="card" @submit.prevent="saveRotation">
        <h2>自动轮换节点</h2>
        <fieldset :disabled="!editable || op.busy.value || resource.data.value.rotation_locked">
          <div class="toolbar">
            <label><input v-model="rotation.enabled" type="checkbox" />开启自动轮换</label
            ><label
              >每处理视频数<input
                v-model.number="rotation.every"
                type="number"
                min="1"
                max="1000"
                required /></label
            ><label
              ><input v-model="rotation.count_failures" type="checkbox" />失败任务计入次数</label
            >
          </div>
          <div class="check-grid">
            <label v-for="node in resource.data.value.nodes" :key="node.name"
              ><input v-model="rotation.nodes" type="checkbox" :value="node.name" />{{
                node.name
              }}</label
            >
          </div>
          <p>轮换顺序：{{ rotation.nodes.join(' → ') || '至少选择两个节点' }}</p>
          <p class="muted">
            各下载进程独立计数；跳过的视频不计数。修改配置或手动切换节点会重新计数。
          </p>
          <button :disabled="!editable || !rotationDirty">保存轮换配置</button>
        </fieldset>
        <p v-for="worker in resource.data.value.rotation.workers" :key="worker.slot">
          进程 {{ worker.slot + 1 }}：已处理 {{ worker.completed }}，下一节点 {{ worker.next_node }}
        </p>
      </form>
      <section class="card">
        <h2>订阅节点</h2>
        <div class="toolbar">
          <input
            v-model="query"
            type="search"
            aria-label="搜索节点"
            placeholder="节点名称 / 协议"
          /><select v-model="sort" aria-label="节点排序">
            <option value="original">订阅顺序</option>
            <option value="delay">延迟从低到高</option>
            <option value="name">名称排序</option></select
          ><button
            :disabled="!editable || op.busy.value || resource.data.value.testing.running"
            @click="action('/api/clash/delay')"
          >
            全部测速
          </button>
        </div>
        <p v-if="resource.data.value.testing.running" role="status">
          测速中
          {{ resource.data.value.testing.done ?? 0 }} /
          {{ resource.data.value.testing.total }}
        </p>
        <p class="muted">延迟测试不切换当前节点，结果不代表下载速度。</p>
        <div class="table-scroll">
          <table>
            <thead>
              <tr>
                <th>节点</th>
                <th>协议</th>
                <th>延迟 / 冷却</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="node in nodes" :key="node.name">
                <td>
                  {{ node.name }}{{ node.name === resource.data.value.selected ? '（当前）' : '' }}
                </td>
                <td>{{ node.type }}</td>
                <td>
                  {{ node.delay === null ? '未测' : node.delay <= 0 ? '超时' : node.delay + ' ms'
                  }}<small v-if="node.cooling_until > Date.now() / 1000" class="cell-note"
                    >冷却到 {{ new Date(node.cooling_until * 1000).toLocaleString('zh-CN') }}</small
                  >
                </td>
                <td>
                  <button
                    :disabled="
                      !editable || op.busy.value || resource.data.value.locked || rotationDirty
                    "
                    @click="action('/api/clash/select', { name: node.name })"
                  >
                    使用节点</button
                  ><button
                    :disabled="!editable || op.busy.value || resource.data.value.testing.running"
                    @click="action('/api/clash/delay', { name: node.name })"
                  >
                    测速
                  </button>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </section>
    </template>
  </main>
</template>
