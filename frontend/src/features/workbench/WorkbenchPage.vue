<script setup lang="ts">
import { computed, ref, watch } from 'vue';
import { RouterLink, useRoute, useRouter } from 'vue-router';
import { api } from '../../api';
import { bootstrap } from '../../bootstrap';
import { useInstances } from '../../stores/instances';
import { useResource } from '../../composables/resource';
import { usePolling } from '../../composables/polling';
import { useLeaveGuard } from '../../composables/leaveGuard';
import InstancePicker from '../../components/InstancePicker.vue';
import SystemNotice from '../../components/SystemNotice.vue';
import TaskLog from '../../components/TaskLog.vue';
import WorkspaceDialog from '../../components/WorkspaceDialog.vue';
import CapacityPanel from './CapacityPanel.vue';
import WorkbenchSpeeds from './WorkbenchSpeeds.vue';
import { stageLabel, type Capacity, type WorkbenchStatus, type RuntimeTask } from './contracts';
import { formatGB, jobStates, videoStates, type Job, type Page } from '../jobs/contracts';
const route = useRoute(),
  router = useRouter(),
  instances = useInstances(),
  editable = bootstrap().role !== 'viewer';
const ready = ref(false),
  busy = ref(false),
  message = ref(''),
  failure = ref('');
const selection = ref(typeof route.query.job === 'string' ? route.query.job : '');
const query = ref(''),
  jobs = useResource<Page<Job>>(),
  workbench = useResource<WorkbenchStatus>(),
  status = useResource<Capacity>();
const jobItems = computed(() => jobs.data.value?.items || []),
  log = ref<RuntimeTask>(),
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
    !!(job.value?.queued || job.value?.running || job.value?.importing) &&
    !alreadyStarted.value &&
    !(queue.value?.stop_requested && queue.value.running),
);
const points = computed(() => queue.value?.session?.output_history?.points || []);
const chart = computed(() => {
  const maximum = Math.max(1, ...points.value.map((item) => item.output_mbps || 0));
  return points.value
    .map(
      (point, index) =>
        `${(index * 600) / Math.max(1, points.value.length - 1)},${100 - ((point.output_mbps || 0) / maximum) * 90}`,
    )
    .join(' ');
});
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
let refreshTicket = 0;
let lastStarted: string | null | undefined;
async function refresh(signal?: AbortSignal) {
  if (!ready.value || busy.value) return;
  const scope = instances.scope(),
    selected = selection.value;
  const ticket = ++refreshTicket;
  await workbench.load(
    (signal) =>
      api.request(
        '/api/jobs/workbench' + (selected ? '?job_id=' + encodeURIComponent(selected) : ''),
        { scope, signal },
      ),
    signal,
  );
  if (
    ticket !== refreshTicket ||
    !scope.isCurrent() ||
    selected !== selection.value ||
    workbench.error.value ||
    !queue.value
  )
    return;
  if (
    (!selected || (lastStarted !== undefined && lastStarted !== queue.value.selected_job_id)) &&
    queue.value.selected_job_id
  )
    selection.value = queue.value.selected_job_id;
  lastStarted = queue.value.selected_job_id;
}
async function refreshCapacity(signal?: AbortSignal) {
  if (!ready.value || busy.value) return;
  const scope = instances.scope();
  await status.load((signal) => api.request('/api/status', { scope, signal }), signal);
}
async function instanceChanged() {
  workbench.clear();
  status.clear();
  log.value = undefined;
  stopping.value = false;
  message.value = '';
  failure.value = '';
  if (ready.value) {
    selection.value = '';
    void clearPreviewLink();
  }
  lastStarted = undefined;
  ready.value = !bootstrap().fleetEnabled || !!instances.selectedId;
  if (ready.value) await Promise.all([refresh(), refreshCapacity(), loadJobs()]);
}
async function clearPreviewLink() {
  if (!new URLSearchParams(location.search).has('job')) return;
  const query = Object.fromEntries(new URLSearchParams(location.search));
  delete query.job;
  await router.replace({ path: route.path, query, hash: route.hash });
}
watch(selection, () => {
  workbench.clear();
  void refresh();
});
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
  busy.value = true;
  failure.value = '';
  message.value = '';
  workbench.clear();
  status.clear();
  try {
    await api.request('/api/jobs/workbench', {
      method: 'POST',
      body: { action, job_id: selection.value },
      scope,
    });
    message.value = {
      start: '已启动所选任务。',
      pause: '当前实例已停止领取；正在执行的视频完成后停止。',
      stop: '已请求立即停止当前实例。',
    }[action];
    started = action === 'start';
    stopping.value = false;
  } catch (error) {
    failure.value = error instanceof Error ? error.message : String(error);
  } finally {
    busy.value = false;
    if (started) await clearPreviewLink();
    await Promise.all([refresh(), refreshCapacity()]);
  }
}
usePolling(
  computed(() => ready.value && !busy.value),
  refresh,
  5000,
);
usePolling(
  computed(() => ready.value && !busy.value),
  refreshCapacity,
  10000,
);
</script>
<template>
  <main class="content">
    <div class="page-heading">
      <div>
        <span class="eyebrow">DOWNLOAD WORKBENCH</span>
        <h1>下载工作台</h1>
        <p>为当前实例选择客户任务，查看执行容量与实时等待原因。</p>
      </div>
      <InstancePicker :disabled="busy" @change="instanceChanged" />
    </div>
    <SystemNotice />
    <WorkbenchSpeeds :status="queue" :unavailable="!!workbench.error.value" />
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
              v-if="selection && !jobItems.some((item) => item.id === selection)"
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
          :disabled="!editable || busy || !queue || (queue.paused && !queue.running)"
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
            ? (queue.online ? (queue.paused ? '已暂停领取' : '允许领取任务') : '实例离线') +
              ' · 当前实例处理中视频 ' +
              queue.running +
              ' 个'
            : '正在读取当前实例状态…'
        }}
      </p>
      <p v-if="queue?.error" class="form-error">{{ queue.error }}</p>
    </section>
    <section v-if="queue?.session" class="card">
      <h2>本轮成品产出</h2>
      <p>
        平均 {{ queue.session.output_mbps ?? '—' }} Mbps · 近十分钟
        {{ queue.session.output_recent_mbps ?? '—' }} Mbps · 正式文件
        {{ queue.session.file_count }} 个 / {{ formatGB(queue.session.file_bytes_exact) }}
      </p>
      <p class="muted">按完成文件和本轮经过时间计算，包含测试产出；实时传输速度见顶部汇总。</p>
      <svg
        v-if="points.length"
        class="output-chart"
        viewBox="0 0 600 110"
        role="img"
        aria-label="近三十分钟每分钟成品产出速率"
      >
        <polyline :points="chart" fill="none" stroke="var(--accent)" stroke-width="2" />
      </svg>
      <details>
        <summary>查看每分钟产出</summary>
        <div class="table-scroll">
          <table>
            <thead>
              <tr>
                <th>时间</th>
                <th>完成文件</th>
                <th>产出速率</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="point in points" :key="point.start_at">
                <td>
                  {{ new Date(point.start_at * 1000).toLocaleTimeString('zh-CN')
                  }}{{ point.partial ? '（未满一分钟）' : '' }}
                </td>
                <td>{{ point.file_count }} 个 / {{ formatGB(point.file_bytes) }}</td>
                <td>{{ point.output_mbps ?? '—' }} Mbps</td>
              </tr>
            </tbody>
          </table>
        </div>
      </details>
    </section>
    <p v-if="status.error.value" class="form-error" role="alert">
      执行容量读取失败：{{ status.error.value }} <button @click="refresh()">重试</button>
    </p>
    <CapacityPanel
      v-if="status.data.value"
      :key="instances.epoch"
      :status="status.data.value"
      :unavailable="!!status.error.value"
    />
    <section class="card">
      <div class="section-heading">
        <h2>当前实例执行任务</h2>
        <RouterLink class="text-link" :to="{ path: '/logs', query: instanceQuery }"
          >查看执行记录 ↗</RouterLink
        >
      </div>
      <div class="table-scroll">
        <table id="workbench-tasks">
          <thead>
            <tr>
              <th>视频</th>
              <th>状态 / 阶段</th>
              <th>进度</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="task in queue?.active_tasks" :key="task.id">
              <td>{{ task.title || task.video_id || task.id }}</td>
              <td>
                {{ videoStates[task.status] || task.status }} ·
                {{ stageLabel(task.progress?.stage) }}
              </td>
              <td>
                {{ task.progress?.percent == null ? '—' : task.progress.percent.toFixed(1) + '%' }}
              </td>
              <td>
                <button
                  @click="log = { ...task, instance_id: instances.selectedId || task.instance_id }"
                >
                  查看日志
                </button>
              </td>
            </tr>
            <tr v-if="queue && !queue.active_tasks.length">
              <td colspan="4" class="empty">当前实例没有执行中的视频。</td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>
    <TaskLog v-if="log" :key="log.id" :task="log" @close="log = undefined" />
    <WorkspaceDialog
      v-if="stopping"
      :open="true"
      title="立即停止当前实例"
      :busy="busy"
      @close="stopping = false"
      ><p>
        将终止 {{ instances.name
        }}<template v-if="instances.selectedId">（{{ instances.selectedId }}）</template>
        当前执行中的视频，未完成条目需要重新处理。
      </p>
      <p v-if="failure" role="alert" class="form-error">{{ failure }}</p>
      <button class="danger" :disabled="busy" @click="control('stop')">
        确认立即停止
      </button></WorkspaceDialog
    >
  </main>
</template>
