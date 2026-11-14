<script setup lang="ts">
import { computed, reactive, ref } from 'vue';
import { RouterLink } from 'vue-router';
import { api } from '../../api';
import { useResource } from '../../composables/resource';
import { useOperation } from '../../composables/operation';
import { usePolling } from '../../composables/polling';
import { useLeaveGuard } from '../../composables/leaveGuard';
import { bootstrap } from '../../bootstrap';
interface Instance {
  id: string;
  name: string;
  host: string;
  directory: string;
  online: boolean;
  paused: boolean;
  running: number;
  transferring: number;
  speed: number;
  stop_requested: boolean;
  error: string;
  cookie_count: number;
  selected_job?: { name: string; customer: string };
  concurrency?: { effective_limit: number };
  role: string;
}
interface Fleet {
  items: Instance[];
  global_limit: number;
  instance_id: string;
}
const resource = useResource<Fleet>(),
  op = useOperation(),
  query = ref(''),
  limit = ref(0),
  limitDirty = ref(false),
  names = reactive<Record<string, string>>({});
const dirty = computed(() => limitDirty.value || Object.keys(names).length > 0);
const editable = bootstrap().role === 'admin';
const items = computed(
  () =>
    resource.data.value?.items.filter(
      (i) =>
        ['hybrid', 'worker'].includes(i.role) &&
        (!query.value ||
          [i.name, i.id, i.host].join(' ').toLowerCase().includes(query.value.toLowerCase())),
    ) || [],
);
useLeaveGuard(() => ({ dirty: dirty.value, busy: op.busy.value }));
async function refresh(signal?: AbortSignal) {
  if (!bootstrap().fleetEnabled) return;
  await resource.load((s) => api.request('/api/instances', { signal: s }), signal);
  if (!limitDirty.value && resource.data.value) limit.value = resource.data.value.global_limit;
}
async function save(id: string, action?: string) {
  await op.run(async () => {
    if (action === 'stop' && !window.confirm(`立即停止「${id}」的当前下载？`)) return;
    const result = await api.request<{ message: string }>(
      '/api/instances/' + encodeURIComponent(id),
      { method: 'PUT', body: action ? { action } : { name: names[id] }, timeoutMs: 90000 },
    );
    await refresh();
    if (resource.error.value) throw new Error('操作已提交，状态读取失败，请重新读取核对。');
    if (!action) delete names[id];
    op.message.value = result.message;
  });
}
async function saveLimit() {
  await op.run(async () => {
    await api.request('/api/instances/limits', {
      method: 'PUT',
      body: { global_limit: Number(limit.value) },
    });
    await refresh();
    if (resource.error.value || resource.data.value?.global_limit !== Number(limit.value))
      throw new Error('全局上限尚未读回确认，草稿已保留。');
    limitDirty.value = false;
    op.message.value = '全局并发上限已保存。';
  });
}
usePolling(
  computed(() => bootstrap().fleetEnabled && !op.busy.value),
  refresh,
  5000,
);
</script>
<template>
  <main class="content">
    <div class="page-heading">
      <div>
        <span class="eyebrow">DOWNLOAD INSTANCES</span>
        <h1>下载实例</h1>
        <p>每张卡片管理对应实例；全局并发上限影响全部实例。</p>
      </div>
      <button @click="refresh()">刷新</button>
    </div>
    <p v-if="!bootstrap().fleetEnabled" class="notice">当前未启用多实例协作。</p>
    <p v-if="resource.error.value || op.error.value" class="notice error" role="alert">
      {{ resource.error.value || op.error.value }}
    </p>
    <p v-if="op.message.value" class="notice" role="status">{{ op.message.value }}</p>
    <form v-if="resource.data.value" class="card toolbar" @submit.prevent="saveLimit">
      <label
        >全局并发上限<input
          v-model.number="limit"
          type="number"
          min="0"
          max="10000"
          :disabled="!editable"
          required
          @input="limitDirty = true" /></label
      ><span class="muted">0 表示仅按实例额度执行</span
      ><button :disabled="!editable || op.busy.value">保存全局上限</button>
    </form>
    <label>搜索实例<input v-model="query" type="search" placeholder="名称、ID 或主机" /></label>
    <section v-for="item in items" :key="item.id" class="card">
      <div class="page-heading">
        <h2>
          {{ item.name }}
          <small class="muted">{{
            item.id === resource.data.value?.instance_id ? '本机入口' : ''
          }}</small>
        </h2>
        <span>{{
          !item.online ? '离线' : item.paused ? (item.running ? '正在停止' : '已停止') : '已启动'
        }}</span>
      </div>
      <p v-if="item.error" class="form-error">{{ item.error }}</p>
      <p>
        {{
          item.selected_job
            ? item.selected_job.customer + ' / ' + item.selected_job.name
            : '尚未选择任务'
        }}
      </p>
      <p>
        处理中 {{ item.online ? item.running : '—' }} · 传输中
        {{ item.online ? item.transferring : '—' }} · 速度
        {{ item.online ? (item.speed / 1e6).toFixed(2) + ' MB/s' : '—' }} · 有效传输上限
        {{ item.online ? (item.concurrency?.effective_limit ?? '待采样') : '—' }}
      </p>
      <div class="actions">
        <RouterLink
          v-for="[path, label] in [
            ['workbench', '工作台'],
            ['accounts', '账号管理'],
            ['settings', '系统设置'],
            ['deployment', '部署配置'],
          ]"
          :key="path"
          :to="{ path: '/' + path, query: { instance_id: item.id } }"
          >{{ label }}</RouterLink
        >
      </div>
      <div class="actions">
        <button
          :disabled="
            !editable ||
            op.busy.value ||
            !item.online ||
            !item.paused ||
            (item.stop_requested && item.running > 0)
          "
          @click="save(item.id, 'resume')"
        >
          启动</button
        ><button
          :disabled="!editable || op.busy.value || item.paused"
          @click="save(item.id, 'drain')"
        >
          完成当前后停止</button
        ><button
          :disabled="
            !editable || op.busy.value || item.stop_requested || (item.paused && !item.running)
          "
          @click="save(item.id, 'stop')"
        >
          立即停止
        </button>
      </div>
      <details>
        <summary>实例详情与名称</summary>
        <p>ID：{{ item.id }}<br />主机：{{ item.host }}<br />素材目录：{{ item.directory }}</p>
        <form class="toolbar" @submit.prevent="save(item.id)">
          <label
            >显示名称<input
              :value="names[item.id] ?? item.name"
              maxlength="100"
              :disabled="!editable"
              required
              @input="names[item.id] = ($event.target as HTMLInputElement).value" /></label
          ><button :disabled="!editable || op.busy.value || names[item.id] === undefined">
            保存名称
          </button>
        </form>
      </details>
    </section>
    <p v-if="resource.data.value && !items.length" class="empty">暂无符合条件的实例。</p>
  </main>
</template>
