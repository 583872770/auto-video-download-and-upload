<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from 'vue';
import { RouterLink } from 'vue-router';
import { api } from '../../api';
import { useInstances } from '../../stores/instances';
import { useResource } from '../../composables/resource';
import TaskLog from '../../components/TaskLog.vue';
import TransferProgress from '../../components/TransferProgress.vue';
import {
  stageLabel,
  type LiveWorkbench,
  type ProcessRow,
  type QueueGroup,
  type QueuePage,
} from './contracts';
import { sampleNow, waitCountdown } from './waits';

const props = defineProps<{ snapshot?: LiveWorkbench; receivedAt: number; error: string }>();
const instances = useInstances();
const filters = [
  { key: 'downloading', label: '下载中' },
  { key: 'pending_transfer', label: '待传输' },
  { key: 'pending_start', label: '等待开工' },
  { key: 'idle', label: '空闲' },
] as const;
const queueGroups: { key: QueueGroup; label: string }[] = [
  { key: 'processing', label: '等待本地处理' },
  { key: 'disk', label: '等待磁盘空间' },
  { key: 'retry', label: '等待重试' },
  { key: 'ready', label: '等待领取' },
];
const outputs: Record<string, string> = {
  video_only: '视频',
  audio_only: '音频',
  av: '音视频',
  subtitle: '字幕',
  thumbnail: '封面',
  metadata: '视频信息',
};
const filter = ref<ProcessRow['state']>();
const search = ref('');
const log = ref<{ id: string; video_id?: string; instance_id?: string }>();
const expanded = ref<QueueGroup>();
const details = useResource<QueuePage>();
const wallNow = ref(Date.now() / 1000);
const tick = ref(performance.now());
let sampledAt = tick.value;
const timer = setInterval(() => {
  wallNow.value = Date.now() / 1000;
  tick.value = performance.now();
}, 1000);
onUnmounted(() => clearInterval(timer));
watch(
  () => props.receivedAt,
  () => {
    sampledAt = performance.now();
    tick.value = sampledAt;
  },
);
const now = computed(() => sampleNow(props.snapshot?.time || 0, sampledAt, tick.value));
const fresh = computed(
  () => !!props.snapshot && !props.error && wallNow.value - props.receivedAt < 15,
);
const rows = computed(() => props.snapshot?.rows || []);
const network = computed(() => {
  const needle = search.value.trim().toLowerCase();
  return rows.value.filter(
    (row) =>
      !row.processing &&
      (!filter.value || row.state === filter.value) &&
      (!needle ||
        [row.title, row.video_id, row.account].some((field) =>
          field?.toLowerCase().includes(needle),
        )),
  );
});
const processing = computed(() => rows.value.filter((row) => row.processing && row.id));
const onlineProcessing = computed(
  () => processing.value.filter((row) => row.stage !== 'waiting_processing').length,
);
const accountLink = computed(() => ({
  path: '/accounts',
  query: { instance_id: instances.selectedId || undefined },
}));
const diskLink = computed(() => ({
  path: '/logs',
  query: { instance_id: instances.selectedId || undefined },
}));

function step(row: ProcessRow) {
  if (row.state === 'idle') return '可领取任务';
  if (row.wait?.label) return row.wait.label;
  const output = outputs[row.output || ''];
  if (row.stage === 'downloading' && output) return '下载' + output;
  if (row.stage === 'running') return '准备任务';
  if (row.processing && row.stage === 'processing' && output) return '处理' + output;
  return stageLabel(row.stage);
}
function showLog(row: { id?: string | null; video_id?: string | null }) {
  if (row.id)
    log.value = {
      id: row.id,
      video_id: row.video_id || undefined,
      instance_id: instances.selectedId || undefined,
    };
}
async function loadQueue(kind: QueueGroup, more = false) {
  const cursor = more ? details.data.value?.next_cursor : undefined;
  const prior = more ? details.data.value?.items || [] : [];
  const params = new URLSearchParams({ kind });
  if (cursor != null) params.set('cursor', String(cursor));
  const scope = instances.scope();
  await details.load(async (signal) => {
    const value = await api.request<QueuePage>('/api/workbench/queue?' + params, { scope, signal });
    return { ...value, items: [...prior, ...value.items] };
  });
}
function toggleQueue(kind: QueueGroup) {
  const open = expanded.value !== kind;
  details.clear();
  expanded.value = open ? kind : undefined;
  if (open) void loadQueue(kind);
}
</script>

<template>
  <section class="card process-panel" aria-label="进程与任务">
    <div class="section-heading">
      <h2>进程与任务</h2>
      <small v-if="snapshot"
        >{{ fresh ? '更新于' : '上次更新' }}
        {{ new Date(snapshot.time * 1000).toLocaleTimeString('zh-CN') }}</small
      >
    </div>
    <p v-if="!fresh" class="notice" role="status">
      {{ error || (snapshot ? '实时采样已过期，等待实例更新。' : '正在读取进程状态…') }}
    </p>
    <p v-if="fresh && snapshot?.paused" class="notice">实例已暂停领取新任务。</p>
    <div class="process-toolbar">
      <div class="process-filters" role="group" aria-label="筛选下载进程">
        <button
          v-for="item in filters"
          :key="item.key"
          type="button"
          :aria-pressed="filter === item.key"
          @click="filter = filter === item.key ? undefined : item.key"
        >
          {{ item.label }} <strong>{{ fresh ? snapshot?.counts[item.key] : '—' }}</strong>
        </button>
      </div>
      <input
        v-model="search"
        type="search"
        aria-label="搜索视频或账号"
        placeholder="搜索视频标题、ID 或账号"
      />
    </div>
    <div class="table-scroll">
      <table class="process-table" aria-label="下载进程">
        <colgroup>
          <col class="process-slot-column" />
          <col class="process-video-column" />
          <col class="process-step-column" />
          <col />
          <col class="process-action-column" />
        </colgroup>
        <thead>
          <tr>
            <th>进程</th>
            <th>视频与账号</th>
            <th>当前步骤</th>
            <th>进度或等待原因</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in network" :key="row.slot">
            <td class="slot">下载 {{ String(row.number).padStart(2, '0') }}</td>
            <td>
              <span class="process-video-title" :title="row.title || undefined">{{
                row.title || row.video_id || '—'
              }}</span>
              <small v-if="row.title && row.video_id" class="cell-note">{{ row.video_id }}</small>
              <RouterLink v-if="row.account" class="text-link cell-note" :to="accountLink">{{
                row.account
              }}</RouterLink>
              <small v-else-if="row.id" class="cell-note">待分配账号</small>
              <small v-else-if="fresh && row.stage === 'waiting_start'" class="cell-note"
                >尚未领取视频</small
              >
            </td>
            <td>
              {{ fresh ? step(row) : '状态待确认'
              }}<small v-if="fresh && row.language" class="cell-note"
                >字幕 · {{ row.language }}</small
              >
            </td>
            <td>
              <template v-if="fresh && row.state === 'downloading'">
                <TransferProgress :progress="{ ...row.progress, stage: 'downloading' }" />
              </template>
              <template v-else-if="fresh && row.wait">
                {{ row.wait.detail || '—' }}
                <small v-if="row.wait.until" class="cell-note">{{
                  waitCountdown(row.wait.until, now, fresh)
                }}</small>
                <small v-else-if="row.phase_started_at" class="cell-note"
                  >已等待 {{ Math.max(0, Math.floor(now - row.phase_started_at)) }} 秒</small
                >
                <RouterLink
                  v-if="row.wait.code === 'account_review'"
                  class="text-link cell-note"
                  :to="accountLink"
                  >打开账号管理</RouterLink
                >
              </template>
              <span v-else>—</span>
            </td>
            <td><button v-if="row.id" type="button" @click="showLog(row)">查看日志</button></td>
          </tr>
          <tr v-if="!network.length">
            <td colspan="5" class="empty">
              {{
                !snapshot
                  ? '暂无实时采样'
                  : filter || search
                    ? '没有符合条件的进程'
                    : '当前没有下载进程运行'
              }}
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <h3>本地处理</h3>
    <p>
      {{ fresh ? onlineProcessing : '—' }} 个处理中 ·
      {{ fresh ? snapshot?.queue.processing : '—' }} 个排队
    </p>
    <ul v-if="processing.length" class="process-details">
      <li v-for="row in processing" :key="row.slot">
        <span
          >本地处理 {{ String(row.number).padStart(2, '0') }} · {{ row.title || row.video_id
          }}<small class="cell-note">{{ fresh ? step(row) : '状态待确认' }}</small></span
        >
        <button type="button" @click="showLog(row)">查看日志</button>
      </li>
    </ul>
    <h3>队列等待</h3>
    <div v-for="item in queueGroups" :key="item.key" class="queue-group">
      <button type="button" :aria-expanded="expanded === item.key" @click="toggleQueue(item.key)">
        {{ item.label }} <strong>{{ fresh ? snapshot?.queue[item.key] : '—' }}</strong>
      </button>
      <RouterLink
        v-if="item.key === 'disk' && snapshot?.queue.disk"
        class="text-link"
        :to="diskLink"
        >查看空间</RouterLink
      >
      <template v-if="expanded === item.key">
        <p v-if="details.error.value" class="form-error">
          {{ details.error.value }} <button type="button" @click="loadQueue(item.key)">重试</button>
        </p>
        <p v-else-if="details.loading.value && !details.data.value">正在读取任务…</p>
        <p v-else-if="!details.data.value?.items.length">没有这类等待任务。</p>
        <ul v-if="details.data.value?.items.length" class="process-details">
          <li v-for="task in details.data.value.items" :key="task.id">
            <span
              >{{ task.title || task.video_id || task.id
              }}<small v-if="task.reason" class="cell-note">{{ task.reason }}</small
              ><small v-if="task.until" class="cell-note"
                >{{ new Date(task.until * 1000).toLocaleTimeString('zh-CN') }} 后检查</small
              ></span
            >
            <button type="button" @click="showLog(task)">查看日志</button>
          </li>
        </ul>
        <button
          v-if="details.data.value?.next_cursor != null"
          type="button"
          :disabled="details.loading.value"
          @click="loadQueue(item.key, true)"
        >
          加载更多
        </button>
      </template>
    </div>
    <TaskLog v-if="log" :key="log.id" :task="log" @close="log = undefined" />
  </section>
</template>

<style scoped>
.process-panel {
  margin-block: 1rem;
}
.process-toolbar,
.process-filters {
  display: flex;
  flex-wrap: wrap;
  gap: 0.5rem;
  align-items: center;
}
.process-toolbar {
  justify-content: space-between;
  margin: 0.9rem 0;
}
.process-toolbar input {
  width: min(100%, 16rem);
}
.process-filters button[aria-pressed='true'] {
  color: white;
  background: #165f9a;
  border-color: #165f9a;
}
.process-filters strong,
.queue-group strong {
  margin-inline-start: 0.3rem;
  font-variant-numeric: tabular-nums;
}
.process-table {
  table-layout: fixed;
}
.process-slot-column {
  width: 96px;
}
.process-video-column {
  width: 26%;
}
.process-step-column {
  width: 18%;
}
.process-action-column {
  width: 112px;
}
.process-table td {
  overflow-wrap: anywhere;
}
.process-video-title {
  display: block;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.slot {
  white-space: nowrap;
  font-variant-numeric: tabular-nums;
}
.queue-group {
  padding-block: 0.4rem;
}
.queue-group .text-link {
  margin-inline-start: 0.8rem;
}
.process-details {
  padding: 0;
  list-style: none;
}
.process-details li {
  display: flex;
  justify-content: space-between;
  align-items: start;
  gap: 0.75rem;
  padding: 0.5rem 0;
}
.process-details li button {
  flex-shrink: 0;
}
</style>
