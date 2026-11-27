<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue';
import { RouterLink, useRoute, useRouter } from 'vue-router';
import { api } from '../../api';
import { bootstrap } from '../../bootstrap';
import { useInstances } from '../../stores/instances';
import { useHeaderStatus } from '../../stores/headerStatus';
import { useResource } from '../../composables/resource';
import { usePolling } from '../../composables/polling';
import { useLeaveGuard } from '../../composables/leaveGuard';
import InstancePicker from '../../components/InstancePicker.vue';
import SystemNotice from '../../components/SystemNotice.vue';
import WorkspaceDialog from '../../components/WorkspaceDialog.vue';
import CapacityPanel from './CapacityPanel.vue';
import DeliverySpeedPanel from './DeliverySpeedPanel.vue';
import ProcessPanel from './ProcessPanel.vue';
import WorkbenchSpeeds from './WorkbenchSpeeds.vue';
import type { Capacity, LiveWorkbench, WorkbenchStatus } from './contracts';
import { jobStates, type Job, type Page } from '../jobs/contracts';
const route = useRoute(),
  router = useRouter(),
  instances = useInstances(),
  headerStatus = useHeaderStatus(),
  editable = bootstrap().role !== 'viewer';
const ready = ref(false),
  localBusy = ref(false),
  message = ref(''),
  failure = ref('');
const busy = computed(() => localBusy.value || headerStatus.busy);
const selection = ref(
  typeof route.query.job === 'string' ? route.query.job : headerStatus.selection,
);
const query = ref(''),
  jobs = useResource<Page<Job>>(),
  workbench = useResource<WorkbenchStatus>(),
  status = useResource<Capacity>(),
  live = useResource<LiveWorkbench>();
const jobItems = computed(() => jobs.data.value?.items || []),
  stopping = ref(false);
const job = computed(() => workbench.data.value?.job);
const queue = computed(() => workbench.data.value);
const selectedMatches = computed(() => !!job.value && job.value.id === selection.value);
const alreadyStarted = computed(
  () =>
    queue.value?.selected_job_id === selection.value && !queue.value?.paused && !!selection.value,
);
const canStart = computed(
  () =>
    editable &&
    !busy.value &&
    !workbench.loading.value &&
    !workbench.error.value &&
    selectedMatches.value &&
    queue.value?.online &&
    !job.value?.archived &&
    !job.value?.deleted &&
    !!(job.value?.queued || job.value?.running || job.value?.importing) &&
    !alreadyStarted.value &&
    !queue.value?.cleaning_temporary &&
    !(queue.value?.stop_requested && queue.value.running),
);
const instanceQuery = computed(() =>
  instances.selectedId ? { instance_id: instances.selectedId } : {},
);
useLeaveGuard(() => ({ dirty: false, busy: busy.value }));
async function loadJobs(more = false) {
  const previous = jobs.data.value?.items || [];
  const params = new URLSearchParams({ q: query.value, limit: '50' });
  if (more && jobs.data.value?.next_cursor) params.set('cursor', jobs.data.value.next_cursor);
  await jobs.load(async (signal) => {
    const result = await api.request<Page<Job>>('/api/jobs?' + params, { signal });
    return { ...result, items: more ? [...previous, ...result.items] : result.items };
  });
}
function refreshJobChoices() {
  if (ready.value && !busy.value) void loadJobs();
}
onMounted(() => window.addEventListener('focus', refreshJobChoices));
onUnmounted(() => window.removeEventListener('focus', refreshJobChoices));
let refreshTicket = 0;
let lastStarted: string | null | undefined;
async function refresh(signal?: AbortSignal, poll = false): Promise<boolean | undefined> {
  if (!ready.value || busy.value) return undefined;
  const scope = instances.scope(),
    selected = selection.value;
  const ticket = ++refreshTicket;
  let received: WorkbenchStatus | undefined;
  const loaded = await workbench.load(async (signal) => {
    const params = new URLSearchParams({ summary: '1' });
    if (selected) params.set('job_id', selected);
    if (poll) params.set('poll', '1');
    const value = await api.request<WorkbenchStatus>(
      '/api/jobs/workbench' + (params.size ? '?' + params : ''),
      { scope, signal, timeoutMs: poll ? 12000 : 30000 },
    );
    received = value;
    return value;
  }, signal);
  if (loaded !== true) return loaded;
  if (
    ticket !== refreshTicket ||
    !scope.isCurrent() ||
    selected !== selection.value ||
    workbench.error.value ||
    !queue.value
  )
    return undefined;
  // Adopt only the response that won the resource race. An older request
  // must never overwrite the shared top-bar snapshot.
  if (received && scope.isCurrent()) headerStatus.adopt(received);
  const previousStarted = lastStarted;
  lastStarted = queue.value.selected_job_id;
  if (selected && !job.value) {
    selection.value = '';
    message.value = '所选任务已删除或不存在，请重新选择任务。';
    void clearPreviewLink();
    await loadJobs();
    return true;
  }
  if (
    (!selected ||
      (previousStarted !== undefined && previousStarted !== queue.value.selected_job_id)) &&
    queue.value.selected_job_id
  )
    selection.value = queue.value.selected_job_id;
  return true;
}
async function refreshCapacity(signal?: AbortSignal, poll = false): Promise<boolean | undefined> {
  if (!ready.value || busy.value) return undefined;
  const scope = instances.scope();
  const result = await status.load(
    (signal) => api.request('/api/status', { scope, signal, timeoutMs: poll ? 12000 : 30000 }),
    signal,
  );
  const capacity = status.data.value?.concurrency;
  if (result && scope.isCurrent() && capacity?.cookie_allocation) {
    headerStatus.adoptAccountSummary({
      sample_time: capacity.sample_time || null,
      accounts: capacity.cookie_allocation.accounts,
    });
  }
  return result;
}
async function instanceChanged() {
  workbench.clear();
  status.clear();
  live.clear();
  stopping.value = false;
  message.value = '';
  failure.value = '';
  if (ready.value) {
    selection.value = '';
    void clearPreviewLink();
  }
  lastStarted = undefined;
  ready.value = !bootstrap().fleetEnabled || !!instances.selectedId;
  if (ready.value) {
    if (selection.value) headerStatus.choose(selection.value);
    await Promise.all([refresh(), refreshCapacity(), loadJobs()]);
  }
}
async function clearPreviewLink() {
  if (!new URLSearchParams(location.search).has('job')) return;
  const query = Object.fromEntries(new URLSearchParams(location.search));
  delete query.job;
  await router.replace({ path: route.path, query, hash: route.hash });
}
watch(selection, () => {
  headerStatus.choose(selection.value);
  workbench.clear();
  void refresh();
});
watch(
  () => headerStatus.selection,
  (value) => {
    if (ready.value && !busy.value) selection.value = value;
  },
);
watch(
  () => headerStatus.controlVersion,
  () => {
    if (!busy.value) void Promise.all([refresh(), refreshCapacity()]);
  },
);
watch(
  () => route.query.job,
  (value) => {
    if (typeof value === 'string') selection.value = value;
  },
);
async function control(action: 'start' | 'pause' | 'stop') {
  if (!editable || busy.value || !queue.value || (action === 'start' && !canStart.value)) return;
  const scope = instances.scope();
  let started = false;
  localBusy.value = true;
  headerStatus.busy = true;
  failure.value = '';
  message.value = '';
  workbench.clear();
  status.clear();
  live.clear();
  try {
    await api.request('/api/jobs/workbench', {
      method: 'POST',
      body: { action, job_id: selection.value },
      scope,
    });
    message.value = {
      start: '已启动所选任务。',
      pause: '当前实例已暂停领取新任务，当前下载和本地待处理内容将继续完成。',
      stop: '正在停止实例并清理未完成视频的临时文件。',
    }[action];
    started = action === 'start';
    stopping.value = false;
  } catch (error) {
    failure.value = error instanceof Error ? error.message : String(error);
  } finally {
    headerStatus.busy = false;
    await headerStatus.invalidate();
    localBusy.value = false;
    if (started) await clearPreviewLink();
    await Promise.all([refresh(), refreshCapacity()]);
  }
}
usePolling(
  computed(() => ready.value && !busy.value),
  (signal) => refresh(signal, true),
  computed(() => {
    if (!queue.value?.online || queue.value.paused) return 30000;
    return 20000;
  }),
);
usePolling(
  computed(() => ready.value && !busy.value),
  (signal) => refreshCapacity(signal, true),
  30000,
);
usePolling(
  computed(() => ready.value),
  (signal) =>
    live.load(
      (signal) =>
        api.request<LiveWorkbench>('/api/workbench/live', {
          scope: instances.scope(),
          signal,
          timeoutMs: 5000,
        }),
      signal,
    ),
  computed(() => (live.data.value?.rows.some((row) => row.id) ? 3000 : 5000)),
);
watch([live.lastSuccessAt, live.error], () => {
  headerStatus.adoptLiveDownload(
    live.data.value?.transfer_summary.speed || 0,
    live.error.value ? 0 : live.lastSuccessAt.value,
    live.data.value?.disk_waiting,
  );
});
</script>
<template>
  <main class="content">
    <div class="page-heading">
      <div>
        <span class="eyebrow">DOWNLOAD WORKBENCH</span>
        <h1>下载工作台</h1>
        <p>为当前实例选择客户任务，查看执行容量与实时等待原因。</p>
      </div>
    </div>
    <InstancePicker :disabled="busy" purpose="operate" @change="instanceChanged" />
    <SystemNotice />
    <WorkbenchSpeeds
      :status="queue"
      :received-at="workbench.lastSuccessAt.value"
      :live="live.data.value"
      :live-received-at="live.lastSuccessAt.value"
      :live-error="live.error.value"
    />
    <p v-if="failure" class="form-error" role="alert">{{ failure }}</p>
    <p v-if="message" class="notice" role="status">{{ message }}</p>
    <section class="card download-console">
      <h2>
        当前实例 · {{ instances.name
        }}<small v-if="instances.selectedId" class="cell-note">{{ instances.selectedId }}</small>
      </h2>
      <div class="download-picker">
        <form class="toolbar" @submit.prevent="loadJobs()">
          <input
            id="download-job-search"
            v-model="query"
            :disabled="busy"
            aria-label="搜索下载任务"
            placeholder="搜索客户或任务名称"
            maxlength="200"
          /><button :disabled="busy || jobs.loading.value">搜索任务</button
          ><button
            id="download-jobs-more"
            type="button"
            :disabled="!jobs.data.value?.next_cursor || jobs.loading.value || busy"
            @click="loadJobs(true)"
          >
            加载更多任务
          </button>
        </form>
        <p v-if="jobs.error.value" class="form-error" role="alert">{{ jobs.error.value }}</p>
        <label
          >下载任务<select
            id="download-job"
            v-model="selection"
            :disabled="busy"
            @change="clearPreviewLink"
          >
            <option value="">请选择任务</option>
            <option
              v-if="selection && job && !jobItems.some((item) => item.id === selection)"
              :value="selection"
            >
              {{ job?.name || selection }}
            </option>
            <option v-for="item in jobItems" :key="item.id" :value="item.id">
              {{ item.customer }} / {{ item.name }}{{ item.archived ? '（已归档）' : '' }}
            </option>
          </select></label
        >
        <p v-if="workbench.error.value" class="form-error" role="alert">
          {{ workbench.error.value }} <button @click="refresh()">重试</button>
        </p>
        <p v-if="busy" role="status">正在检查或控制当前实例，请稍候…</p>
        <p id="download-job-note">
          {{
            job
              ? job.customer + ' / ' + job.name
              : selection
                ? '正在读取所选任务…'
                : '请选择任务，选择本身不会开始下载。'
          }}
        </p>
      </div>
      <div v-if="job && selectedMatches" class="download-preview">
        <div class="summary-grid">
          <div class="card">
            <small>待处理</small><strong>{{ job.queued }}</strong>
          </div>
          <div class="card">
            <small>当前实例处理中</small><strong>{{ job.running }}</strong>
          </div>
          <div class="card">
            <small>任务处理成功 / 失败</small
            ><strong>{{ job.succeeded }} / {{ job.failed }}</strong>
          </div>
          <div class="card">
            <small>任务状态</small
            ><strong class="metric-state">{{ jobStates[job.state] || job.state }}</strong>
          </div>
        </div>
        <p v-if="job.delivery?.contract">
          交付要求版本 {{ job.delivery.contract.version }} · 执行成功后仍需核对正式交付文件。
        </p>
        <RouterLink id="download-job-detail" class="text-link" :to="'/jobs/' + job.id"
          >查看任务与交付要求 ↗</RouterLink
        >
      </div>
      <p id="download-selection-status">
        {{
          queue?.selected_job_id && queue.selected_job_id !== selection
            ? '点击启动后，当前实例将切换到所选任务。'
            : job?.waiting_reason ||
              (alreadyStarted ? '已启动，按当前资源条件领取视频。' : '选择任务后点击启动开始处理。')
        }}
      </p>
      <div class="actions">
        <button
          id="download-queue-start"
          class="primary"
          :disabled="!canStart"
          @click="control('start')"
        >
          {{ busy ? '处理中…' : alreadyStarted ? '已启动' : '启动' }}</button
        ><button
          id="download-job-pause"
          :disabled="!editable || busy || !queue || queue.paused"
          @click="control('pause')"
        >
          暂停领取</button
        ><button
          id="download-job-stop"
          class="danger"
          :disabled="!editable || busy || !queue || queue.cleaning_temporary"
          @click="stopping = true"
        >
          立即停止</button
        ><RouterLink
          class="text-link"
          :to="{ path: '/settings', query: instanceQuery, hash: '#download' }"
          >修改当前实例策略 ↗</RouterLink
        >
      </div>
      <p id="download-queue-status">
        {{
          queue
            ? (queue.online
                ? queue.cleaning_temporary
                  ? '正在停止并清理临时文件'
                  : queue.paused
                    ? '已暂停领取'
                    : '允许领取任务'
                : '实例离线') +
              ' · 当前实例处理中视频 ' +
              queue.running +
              ' 个'
            : '正在读取当前实例状态…'
        }}
      </p>
      <p v-if="queue?.error" class="form-error">{{ queue.error }}</p>
    </section>
    <DeliverySpeedPanel v-if="ready" :key="instances.epoch" />
    <p v-if="status.error.value" class="form-error" role="alert">
      执行容量读取失败：{{ status.error.value }} <button @click="refresh()">重试</button>
    </p>
    <CapacityPanel
      v-if="status.data.value"
      :key="instances.epoch"
      :status="status.data.value"
      :received-at="status.lastSuccessAt.value"
    />
    <ProcessPanel
      :key="'processes-' + instances.epoch"
      :snapshot="live.data.value"
      :received-at="live.lastSuccessAt.value"
      :error="live.error.value"
    />
    <WorkspaceDialog
      v-if="stopping"
      :open="true"
      title="立即停止当前实例"
      :busy="busy"
      @close="stopping = false"
      ><p>
        将终止 {{ instances.name
        }}<template v-if="instances.selectedId">（{{ instances.selectedId }}）</template>
        的下载和本地处理，并删除本地待处理和下载中的临时文件。下次开始时重新下载。
      </p>
      <p v-if="failure" role="alert" class="form-error">{{ failure }}</p>
      <button class="danger" :disabled="busy" @click="control('stop')">
        确认立即停止
      </button></WorkspaceDialog
    >
  </main>
</template>
