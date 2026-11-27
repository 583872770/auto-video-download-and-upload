<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue';
import { RouterLink, useRoute, useRouter } from 'vue-router';
import { messageBox as ElMessageBox } from '../composables/dialogs';
import { bootstrap } from '../bootstrap';
import { api } from '../api';
import { useInstances } from '../stores/instances';
import { useHeaderStatus } from '../stores/headerStatus';
import { useNotifications } from '../stores/notifications';
import { usePolling } from '../composables/polling';
import { confirmNavigation, useLeaveGuard } from '../composables/leaveGuard';
import { useResource } from '../composables/resource';
import { speedNumber } from '../features/workbench/speeds';
import type { Job, Page } from '../features/jobs/contracts';
import AppIcon from './AppIcon.vue';
import { diskBytes, diskLabel } from '../features/disk/model';

defineProps<{ menuOpen: boolean }>();
const emit = defineEmits<{ menu: []; logout: []; refreshPage: [] }>();
const instances = useInstances(),
  status = useHeaderStatus(),
  notifications = useNotifications(),
  router = useRouter(),
  route = useRoute();
const pickerRoot = ref<HTMLElement>(),
  moreRoot = ref<HTMLElement>(),
  resourceRoot = ref<HTMLElement>(),
  picker = ref(false),
  more = ref(false),
  resourceOpen = ref<'buffer' | 'disk'>();
const search = ref(''),
  jobs = useResource<Page<Job>>();
const ready = computed(() => !bootstrap().fleetEnabled || !!instances.selectedId);
const onWorkbench = computed(() => route.path === '/workbench');
const blocked = computed(() => status.busy);
useLeaveGuard(() => ({ dirty: false, busy: status.busy }));
async function loadJobs(append = false) {
  const previous = jobs.data.value?.items || [];
  const params = new URLSearchParams({ q: search.value, limit: '50' });
  if (append && jobs.data.value?.next_cursor) params.set('cursor', jobs.data.value.next_cursor);
  await jobs.load(async (signal) => {
    const value = await api.request<Page<Job>>('/api/jobs?' + params, { signal });
    return { ...value, items: append ? [...previous, ...value.items] : value.items };
  });
}
function openPicker() {
  if (blocked.value) return;
  picker.value = !picker.value;
  more.value = false;
  if (picker.value) void loadJobs();
}
function choose(id: string) {
  status.choose(id);
  picker.value = false;
}
async function switchInstance(event: Event) {
  const input = event.target as HTMLSelectElement,
    id = input.value;
  input.value = instances.selectedId;
  if (id === instances.selectedId || blocked.value) return;
  picker.value = more.value = false;
  // Route navigation runs the existing draft guard before changing the scope.
  // Page-level pickers also update browser history directly. Force navigation
  // so returning to the router's previous query still runs the draft guard.
  const sameQuery = route.query.instance_id === id;
  // Settings permits same-scope route changes for its category tabs. A page
  // picker may have changed the actual scope since that route was recorded.
  if (sameQuery && route.path === '/settings' && !(await confirmNavigation())) return;
  const failure = await router.replace({
    force: true,
    path: route.path,
    query: {
      ...Object.fromEntries(new URLSearchParams(location.search)),
      instance_id: id,
      ...(route.path === '/workbench' ? { job: undefined } : {}),
    },
    hash: route.hash,
  });
  if (!failure) {
    instances.select(id);
    if (sameQuery) emit('refreshPage');
  }
}
watch(
  () => route.query.instance_id,
  (id) => {
    if (typeof id === 'string' && instances.items.some((item) => item.id === id))
      instances.select(id);
  },
  { flush: 'sync' },
);
async function toggle() {
  if (status.enabled) return status.control('pause');
  if (!status.canStart) return;
  if (
    status.data?.selected_job_id &&
    status.data.selected_job_id !== status.selection &&
    (!status.data.paused || status.data.running)
  ) {
    try {
      await ElMessageBox.confirm(
        '切换任务将停止本实例原任务的执行，再启动所选任务。',
        '切换运行任务',
        { confirmButtonText: '切换并启动', cancelButtonText: '取消', type: 'warning' },
      );
    } catch {
      return;
    }
  }
  await status.control('start');
}
async function stop() {
  more.value = false;
  if (!status.canStop) return;
  try {
    await ElMessageBox.confirm(`立即停止「${instances.name}」正在执行的任务？`, '立即停止', {
      confirmButtonText: '立即停止',
      cancelButtonText: '取消',
      type: 'warning',
    });
  } catch {
    return;
  }
  await status.control('stop');
}
function outside(event: PointerEvent) {
  if (!pickerRoot.value?.contains(event.target as Node)) picker.value = false;
  if (!moreRoot.value?.contains(event.target as Node)) more.value = false;
  if (!resourceRoot.value?.contains(event.target as Node)) resourceOpen.value = undefined;
}
function escape(event: KeyboardEvent) {
  if (event.key === 'Escape') {
    picker.value = more.value = false;
    resourceOpen.value = undefined;
  }
}
let timer: ReturnType<typeof setInterval>;
let feedbackTimer: ReturnType<typeof setTimeout> | undefined;
watch(
  () => status.message,
  (value) => {
    clearTimeout(feedbackTimer);
    if (value)
      feedbackTimer = setTimeout(() => {
        status.message = '';
      }, 7000);
  },
);
onMounted(() => {
  void instances.load().then(() => {
    if (!onWorkbench.value) void status.refresh();
  });
  timer = setInterval(() => {
    status.now = Date.now() / 1000;
  }, 1000);
  document.addEventListener('pointerdown', outside);
  document.addEventListener('keydown', escape);
});
onUnmounted(() => {
  clearInterval(timer);
  clearTimeout(feedbackTimer);
  status.clear();
  document.removeEventListener('pointerdown', outside);
  document.removeEventListener('keydown', escape);
});
usePolling(
  computed(() => ready.value && !status.busy && !onWorkbench.value),
  (signal) => status.refresh(signal, true),
  computed(() => {
    if (!status.data?.online || status.data.paused) return 30000;
    return status.data.running ? 5000 : 15000;
  }),
);
watch(
  () => instances.selectedId,
  () => {
    picker.value = more.value = false;
    resourceOpen.value = undefined;
  },
);
const disk = computed(() => (status.displayFresh ? status.data?.disk : null));
const diskQuota = computed(() => {
  const total = disk.value?.download?.constraints?.find((row) => row.key === 'total');
  if (!total || total.limit_bytes <= 0) return null;
  const percent = (bytes: number) => Math.min(100, (100 * bytes) / total.limit_bytes);
  return {
    ...total,
    writtenPercent: percent(total.used_bytes),
    committedPercent: percent(total.committed_bytes),
  };
});
const diskCategories = computed(() =>
  (disk.value?.download?.constraints || []).filter(
    (row) => row.key === 'test' || row.key === 'downloads' || (row.key !== 'total' && row.limiting),
  ),
);
const bufferFullyAllocated = computed(
  () => !!status.hlsBuffer && status.hlsBuffer.occupied_slots === status.hlsBuffer.total_slots,
);
function quantityPair(used: number, limit: number, power: number) {
  const scale = 1024 ** power;
  const maximum = limit / scale;
  return `${(used / scale).toFixed(1)} / ${maximum.toFixed(Number.isInteger(maximum) ? 0 : 1)} ${power === 2 ? 'MiB' : 'GiB'}`;
}
function toggleResource(resource: 'buffer' | 'disk') {
  resourceOpen.value = resourceOpen.value === resource ? undefined : resource;
}
const powerTitle = computed(
  () =>
    status.error ||
    status.data?.error ||
    (!status.fresh
      ? '等待当前实例的新鲜状态'
      : !status.selection
        ? '请先选择任务'
        : status.enabled
          ? '暂停领取新任务，当前下载完成后停止'
          : !status.canStart
            ? '所选任务没有待处理内容，或暂时无法启动'
            : '启动所选任务'),
);
</script>

<template>
  <header class="topbar workspace-statusbar" aria-label="实例任务状态栏">
    <div class="header-controls">
      <button
        type="button"
        class="mobile-menu"
        aria-label="打开导航"
        :aria-expanded="menuOpen"
        @click="emit('menu')"
      >
        <AppIcon name="menu" />
      </button>
      <div class="header-instance" :title="instances.error || '当前查看和操作的下载实例'">
        <i
          class="header-dot"
          :class="{ online: status.displayFresh, stale: status.displayFresh && !status.fresh }"
          aria-hidden="true"
        ></i>
        <select
          v-if="bootstrap().fleetEnabled"
          id="header-instance"
          aria-label="顶部当前实例"
          :value="instances.selectedId"
          :disabled="blocked || !instances.items.length"
          @change="switchInstance"
        >
          <option v-if="!instances.items.length" value="">
            {{ instances.error ? '实例读取失败' : '读取实例…' }}
          </option>
          <option v-for="item in instances.items" :key="item.id" :value="item.id">
            {{ item.name }}
          </option>
        </select>
        <span v-else>{{ instances.name }}</span>
        <span class="header-chevron" aria-hidden="true">⌄</span>
      </div>
      <div ref="pickerRoot" class="header-task-wrap">
        <button
          id="header-task"
          class="header-task"
          :disabled="blocked || !ready"
          :aria-expanded="picker"
          aria-controls="header-task-picker"
          :title="
            status.chosen
              ? status.chosen.customer + ' / ' + status.chosen.name
              : '选择任务，选择本身不会启动下载'
          "
          @click="openPicker"
        >
          <span>{{ status.chosen?.name || (status.selection ? '读取任务…' : '请选择任务') }}</span
          ><span class="header-chevron" aria-hidden="true">⌄</span>
        </button>
        <div
          v-if="picker"
          id="header-task-picker"
          class="header-popover header-task-picker"
          role="dialog"
          aria-label="选择运行任务"
        >
          <div class="header-picker-heading">
            <strong>选择任务</strong><small>选择后，打开开关才会启动</small>
          </div>
          <form @submit.prevent="loadJobs()">
            <input
              v-model="search"
              aria-label="顶部搜索任务"
              placeholder="搜索客户或任务名称"
              maxlength="200"
            /><button :disabled="jobs.loading.value">搜索</button>
          </form>
          <p v-if="jobs.error.value" class="form-error" role="alert">{{ jobs.error.value }}</p>
          <div class="header-job-list" role="group" aria-label="可选任务">
            <button
              v-for="job in jobs.data.value?.items || []"
              :key="job.id"
              :disabled="job.archived || job.deleted"
              :class="{ selected: status.selection === job.id }"
              @click="choose(job.id)"
            >
              <span>{{ job.name }}</span
              ><small
                >{{ job.customer }} · 待处理 {{ job.queued
                }}{{ job.archived ? ' · 已归档' : '' }}</small
              >
              <span v-if="status.data?.selected_job_id === job.id" class="header-current-job"
                >当前实例任务</span
              >
            </button>
          </div>
          <p v-if="jobs.loading.value" role="status">正在读取任务…</p>
          <p v-else-if="!jobs.data.value?.items.length">没有找到任务</p>
          <button
            v-if="jobs.data.value?.next_cursor"
            :disabled="jobs.loading.value"
            @click="loadJobs(true)"
          >
            加载更多
          </button>
        </div>
      </div>
      <div class="header-power" :class="{ enabled: status.enabled && status.fresh }">
        <span class="header-state" :title="powerTitle">{{ status.label }}</span>
        <button
          id="header-power"
          class="header-switch"
          type="button"
          role="switch"
          aria-label="启动所选任务"
          :aria-checked="status.enabled && status.fresh"
          :title="powerTitle"
          :disabled="status.enabled ? !status.canPause : !status.canStart"
          @click="toggle"
        >
          <span></span>
        </button>
        <div ref="moreRoot" class="header-more-wrap">
          <button
            class="header-more"
            aria-label="更多任务操作"
            :aria-expanded="more"
            @click="
              more = !more;
              picker = false;
            "
          >
            ⋯
          </button>
          <div v-if="more" class="header-popover header-more-menu">
            <small>作用于 {{ instances.name }}</small>
            <button
              :disabled="!status.canPause"
              @click="
                more = false;
                status.control('pause');
              "
            >
              暂停领取新任务
            </button>
            <button class="header-stop" :disabled="!status.canStop" @click="stop">立即停止…</button>
            <button :disabled="blocked || status.loading" @click="status.refresh()">
              刷新状态
            </button>
            <button v-if="instances.error" @click="instances.load()">重新读取实例</button>
            <p v-if="status.error || instances.error" role="alert">
              {{ status.error || instances.error }}
            </p>
            <p>暂停后当前下载继续，立即停止会中断当前执行。</p>
          </div>
        </div>
      </div>
    </div>
    <div class="header-readings" :title="instances.name + ' · 每 5 秒更新'">
      <span class="header-metric" title="当前实例全部下载任务的实时速度合计"
        ><span>下载</span
        ><strong id="header-download"
          >{{ speedNumber(status.download) }}<small> MB/s</small></strong
        ></span
      >
      <span class="header-metric" title="当前实例会话产出文件总大小 ÷ 会话运行时间"
        ><span>产出</span
        ><strong id="header-output"
          >{{ speedNumber(status.output) }}<small> MB/s</small></strong
        ></span
      >
      <span class="header-cooling" title="当前实例账号池中正在冷却的账号数，重复会话只计一次"
        ><i aria-hidden="true"></i>冷却
        <strong id="header-cooling">{{ status.cooling ?? '—' }}</strong></span
      >
    </div>
    <div ref="resourceRoot" class="header-resources">
      <div class="header-resource-wrap">
        <button
          class="header-resource buffer-meter"
          :class="{ warning: bufferFullyAllocated }"
          type="button"
          aria-controls="header-buffer-details"
          :aria-expanded="resourceOpen === 'buffer'"
          @click="toggleResource('buffer')"
        >
          <span class="resource-heading">
            <span>分片缓冲</span>
            <strong>{{
              bufferFullyAllocated
                ? '额度满'
                : status.hlsBuffer
                  ? status.hlsBuffer.percent + '%'
                  : '—'
            }}</strong>
          </span>
          <span class="resource-track" aria-hidden="true">
            <span :style="{ width: (status.hlsBuffer?.percent ?? 0) + '%' }"></span>
          </span>
          <span class="resource-caption">
            {{
              status.hlsBuffer
                ? quantityPair(status.hlsBuffer.used_bytes, status.hlsBuffer.limit_bytes, 2)
                : '等待采样'
            }}
          </span>
        </button>
        <div
          v-if="resourceOpen === 'buffer'"
          id="header-buffer-details"
          class="header-popover header-resource-details"
          role="region"
          aria-label="分片缓冲详情"
        >
          <strong>{{ instances.name }} · HLS 分片缓冲</strong>
          <dl v-if="status.hlsBuffer">
            <div>
              <dt>已用 / 实例上限</dt>
              <dd>
                {{ quantityPair(status.hlsBuffer.used_bytes, status.hlsBuffer.limit_bytes, 2) }}
              </dd>
            </div>
            <div>
              <dt>单视频额度</dt>
              <dd>{{ diskBytes(status.hlsBuffer.per_file_bytes) }}</dd>
            </div>
            <div>
              <dt>已领额度</dt>
              <dd>{{ status.hlsBuffer.occupied_slots }} / {{ status.hlsBuffer.total_slots }} 个</dd>
            </div>
          </dl>
          <p v-if="status.hlsBuffer">额度按需领取。额度不足或系统内存偏低时，分片自动写入磁盘。</p>
          <p v-else>当前暂无有效的分片缓冲采样。</p>
        </div>
      </div>
      <div class="header-resource-wrap">
        <button
          class="header-resource disk-meter"
          :class="disk?.severity"
          type="button"
          aria-controls="header-disk-quota-details"
          :aria-expanded="resourceOpen === 'disk'"
          :title="diskLabel(disk)"
          @click="toggleResource('disk')"
        >
          <span class="resource-heading">
            <span>临时磁盘</span>
            <strong>{{
              disk?.waiting_tasks
                ? disk.waiting_tasks + ' 等待'
                : diskQuota
                  ? Math.round(diskQuota.committedPercent) + '%'
                  : '—'
            }}</strong>
          </span>
          <span class="resource-track" aria-hidden="true">
            <span
              class="quota-reserved"
              :style="{ width: (diskQuota?.committedPercent ?? 0) + '%' }"
            ></span>
            <span :style="{ width: (diskQuota?.writtenPercent ?? 0) + '%' }"></span>
          </span>
          <span class="resource-caption">
            {{
              diskQuota
                ? quantityPair(diskQuota.committed_bytes, diskQuota.limit_bytes, 3)
                : '等待采样'
            }}
          </span>
        </button>
        <div
          v-if="resourceOpen === 'disk'"
          id="header-disk-quota-details"
          class="header-popover header-resource-details disk-quota-details"
          role="region"
          aria-label="临时磁盘额度详情"
        >
          <strong>{{ instances.name }} · 临时磁盘额度</strong>
          <dl v-if="disk && diskQuota">
            <div>
              <dt>已写入（深色）</dt>
              <dd>{{ diskBytes(diskQuota.used_bytes) }}</dd>
            </div>
            <div>
              <dt>已预留待写入（浅色）</dt>
              <dd>{{ diskBytes(diskQuota.reserved_bytes) }}</dd>
            </div>
            <div>
              <dt>额度剩余</dt>
              <dd>{{ diskBytes(diskQuota.remaining_bytes) }}</dd>
            </div>
            <div>
              <dt>磁盘实际可用</dt>
              <dd>{{ diskBytes(disk.free_bytes) }}</dd>
            </div>
            <div v-for="item in diskCategories" :key="item.key">
              <dt>{{ item.label }}额度</dt>
              <dd>{{ quantityPair(item.committed_bytes, item.limit_bytes, 3) }}</dd>
            </div>
          </dl>
          <p v-if="disk?.download">{{ disk.download.impact_text }}</p>
          <p v-else>当前暂无有效的磁盘额度采样。</p>
          <p v-if="disk?.download && disk.download.impact !== 'unaffected'">
            {{ disk.download.reason }}
            <template v-if="disk.download.shortfall_bytes"
              >{{ disk.download.shortfall_text }}。</template
            >
          </p>
          <button
            class="disk-quota-link"
            type="button"
            @click="
              resourceOpen = undefined;
              router.push({
                path: '/settings',
                query: bootstrap().fleetEnabled ? { instance_id: instances.selectedId } : {},
                hash: '#maintenance',
              });
            "
          >
            查看空间与日志
          </button>
        </div>
      </div>
    </div>
    <button
      class="header-notifications"
      type="button"
      aria-controls="notification-center"
      :aria-expanded="notifications.open"
      :aria-label="
        notifications.error
          ? '通知状态读取失败'
          : `通知，${notifications.globalCounts.action} 项需要处理`
      "
      @click="notifications.open ? notifications.close() : notifications.show()"
    >
      <AppIcon name="bell" /><span>通知</span><b v-if="notifications.error">!</b>
      <b v-else-if="notifications.globalCounts.action">{{
        notifications.globalCounts.action > 99 ? '99+' : notifications.globalCounts.action
      }}</b>
      <i v-else-if="notifications.globalCounts.recovering" class="notification-recovery-dot"></i>
    </button>
    <div class="header-user">
      <RouterLink to="/login">管理账户</RouterLink
      ><button type="button" aria-label="退出登录" @click="emit('logout')">
        <AppIcon name="logout" /><span>退出</span>
      </button>
    </div>
    <div v-if="status.message" class="header-feedback" role="status">
      <span>{{ status.message }}</span
      ><button aria-label="关闭状态提示" @click="status.message = ''">×</button>
    </div>
  </header>
</template>

<style scoped>
.header-resources {
  display: flex;
  gap: 8px;
  flex-shrink: 0;
}
.header-resource-wrap {
  position: relative;
  flex: 0 0 111px;
}
.workspace-statusbar .header-resource {
  --meter-color: #177562;
  display: grid;
  width: 100%;
  gap: 3px;
  padding: 4px 7px;
  border: 1px solid #e5eaf1;
  border-radius: 5px;
  background: #f8fafc;
  color: #475569;
  text-align: left;
  line-height: 1.15;
}
.workspace-statusbar .disk-meter {
  --meter-color: #2463b2;
  --reserve-color: #a5c6ef;
}
.workspace-statusbar .header-resource.warning {
  --meter-color: #946200;
  --reserve-color: #edd091;
  background: #fff8eb;
}
.workspace-statusbar .header-resource.critical,
.workspace-statusbar .header-resource.blocked {
  --meter-color: #b83b36;
  --reserve-color: #efb1ac;
  background: #fff1f0;
}
.resource-heading {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 3px;
  font-size: 10px;
}
.resource-heading strong {
  color: var(--meter-color);
}
.resource-caption {
  font-size: 10px;
}
.resource-heading,
.resource-caption {
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}
.resource-track {
  position: relative;
  display: block;
  overflow: hidden;
  height: 4px;
  border-radius: 2px;
  background: #e2e8f0;
}
.resource-track > span {
  position: absolute;
  inset: 0 auto 0 0;
  background: var(--meter-color);
}
.resource-track > .quota-reserved {
  background: var(--reserve-color);
}
.header-resource-details {
  left: auto;
  right: 0;
  width: 270px;
  max-width: calc(100vw - 32px);
  line-height: 1.5;
  font-size: 12px;
}
.header-resource-details dl {
  margin: 8px 0;
}
.header-resource-details dl > div {
  display: flex;
  justify-content: space-between;
  gap: 8px;
  padding: 4px 0;
}
.header-resource-details dd {
  margin: 0;
  text-align: right;
  font-variant-numeric: tabular-nums;
}
.header-resource-details p {
  margin: 5px 0 0;
  color: #64748b;
}
.workspace-statusbar .disk-quota-link {
  margin-top: 8px;
  padding: 4px 8px;
  min-height: 26px;
  font-size: 11px;
}
@media (max-width: 1330px) {
  .header-metric:nth-child(2) {
    display: none;
  }
}
.header-notifications {
  display: flex;
  align-items: center;
  gap: 6px;
  white-space: nowrap;
  min-height: 30px !important;
  padding: 4px 9px !important;
  background: #eff4ff !important;
  border-color: #e7eefc !important;
  color: #2563eb !important;
  font-size: 11px !important;
}
.header-notifications b {
  border-radius: 4px;
  background: #dc4545;
  color: white;
  font-size: 10px;
  padding: 0 4px;
  line-height: 17px;
}
.notification-recovery-dot {
  width: 6px;
  height: 6px;
  background: #d19a48;
  border-radius: 50%;
}
.workspace-statusbar {
  position: sticky;
  top: 0;
  z-index: 15;
  height: 54px;
  background: #fff;
  gap: 19px;
  color: #475569;
  font-size: 11px;
}
.header-controls {
  display: flex;
  align-items: center;
  gap: 19px;
  min-width: 0;
}
.header-instance {
  display: flex;
  align-items: center;
  gap: 7px;
  background: #f6f8fb;
  border: 1px solid #e5eaf1;
  border-radius: 5px;
  padding: 4px 9px;
  flex-shrink: 0;
  position: relative;
}
.header-dot {
  width: 5px;
  height: 5px;
  border-radius: 50%;
  background: #a6b2c1;
  flex-shrink: 0;
}
.header-dot.online {
  background: #279b85;
}
.header-dot.stale {
  background: #d19a48;
}
.header-instance select {
  appearance: none;
  border: 0;
  background: transparent;
  min-height: 22px;
  height: 22px;
  padding: 0 14px 0 0;
  margin: 0;
  width: auto;
  max-width: 140px;
  border-radius: 0;
  font-size: 11px;
  color: #42556b;
  cursor: pointer;
}
.header-chevron {
  color: #93a2b6;
  font-size: 10px;
  pointer-events: none;
}
.header-instance > .header-chevron {
  position: absolute;
  right: 9px;
}
.header-task-wrap,
.header-more-wrap {
  position: relative;
  min-width: 0;
}
.workspace-statusbar button {
  box-shadow: none;
}
.workspace-statusbar .header-task {
  display: flex;
  align-items: center;
  gap: 13px;
  padding: 0;
  border: 0;
  background: transparent;
  color: #40546c;
  min-height: 30px;
  font-size: 12px;
  font-weight: 500;
  max-width: 230px;
}
.header-task > span:first-child {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.header-power {
  display: flex;
  align-items: center;
  gap: 8px;
  padding-left: 16px;
  border-left: 1px solid #e7ecf2;
  height: 22px;
  color: #8797aa;
  flex-shrink: 0;
}
.header-power.enabled {
  color: #1e8d79;
}
.header-state {
  font-size: 10px;
  white-space: nowrap;
}
.workspace-statusbar .header-switch {
  width: 31px;
  height: 18px;
  min-height: 18px;
  border: 0;
  background: #c5cfdb;
  border-radius: 12px;
  padding: 0;
  position: relative;
  flex-shrink: 0;
}
.header-switch span {
  height: 12px;
  width: 12px;
  position: absolute;
  left: 3px;
  top: 3px;
  background: #fff;
  border-radius: 50%;
  box-shadow: 0 1px 2px #0001;
  transition: left 0.15s;
}
.workspace-statusbar .header-switch[aria-checked='true'] {
  background: #249a84;
}
.header-switch[aria-checked='true'] span {
  left: 16px;
}
.workspace-statusbar .header-more {
  border: 0;
  background: transparent;
  min-height: 24px;
  font-size: 18px;
  color: #8e9fb4;
  padding: 0 2px;
  line-height: 16px;
}
.header-readings {
  margin-left: auto;
  display: flex;
  align-items: center;
  gap: 22px;
  flex-shrink: 0;
}
.header-metric {
  display: flex;
  gap: 8px;
  align-items: baseline;
  white-space: nowrap;
}
.header-metric > span {
  color: #8797aa;
  font-size: 10px;
}
.header-metric strong {
  color: #3e5269;
  font-size: 13px;
  font-weight: 600;
  letter-spacing: -0.15px;
  font-variant-numeric: tabular-nums;
}
.header-metric small {
  font-size: 9px;
  color: #92a1b2;
  font-weight: 400;
}
.header-cooling {
  display: flex;
  align-items: center;
  gap: 5px;
  color: #8998aa;
  font-size: 10px;
  white-space: nowrap;
}
.header-cooling i {
  width: 5px;
  height: 5px;
  border-radius: 50%;
  background: #c5a16b;
  margin-right: 2px;
}
.header-cooling strong {
  color: #ac874c;
  font-size: 12px;
  font-weight: 500;
  font-variant-numeric: tabular-nums;
}
.header-user {
  display: flex;
  align-items: center;
  gap: 13px;
  padding-left: 17px;
  border-left: 1px solid #e7ecf2;
  white-space: nowrap;
  flex-shrink: 0;
}
.header-user a {
  font-size: 10px;
  color: #74859c;
  text-decoration: none;
}
.workspace-statusbar .header-user button {
  display: flex;
  align-items: center;
  gap: 4px;
  border: 0;
  padding: 0;
  background: transparent;
  min-height: 24px;
  font-size: 10px;
  color: #8e9db0;
}
.header-user .app-icon {
  width: 12px;
  height: 12px;
}
.header-popover {
  position: absolute;
  top: calc(100% + 13px);
  left: 0;
  z-index: 30;
  padding: 14px;
  background: #fff;
  border: 1px solid #e2e8f0;
  box-shadow: 0 8px 28px #1e293b1a;
  border-radius: 8px;
  color: #475569;
}
.header-task-picker {
  width: 340px;
  max-width: calc(100vw - 32px);
}
.header-picker-heading {
  display: flex;
  flex-direction: column;
  gap: 4px;
  margin-bottom: 12px;
}
.header-picker-heading strong {
  font-size: 13px;
}
.header-picker-heading small {
  color: #8797aa;
}
.header-task-picker form {
  display: flex;
  gap: 6px;
}
.header-task-picker input {
  min-width: 0;
  width: 100%;
  min-height: 30px;
  font-size: 12px;
}
.workspace-statusbar .header-task-picker button {
  font-size: 12px;
  min-height: 30px;
  padding: 4px 9px;
}
.header-job-list {
  max-height: 300px;
  overflow-y: auto;
  margin-top: 10px;
}
.workspace-statusbar .header-job-list button {
  display: flex;
  position: relative;
  flex-direction: column;
  gap: 4px;
  text-align: left;
  border: 0;
  border-radius: 5px;
  width: 100%;
  padding: 10px;
  background: transparent;
  color: #40546c;
}
.workspace-statusbar .header-job-list button:hover,
.workspace-statusbar .header-job-list button.selected {
  background: #f0f7f5;
}
.header-job-list small {
  font-size: 10px;
  color: #8797aa;
}
.header-current-job {
  font-size: 10px;
  color: #1e8d79;
}
.header-more-menu {
  width: 240px;
  left: auto;
  right: 0;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.workspace-statusbar .header-more-menu > button {
  text-align: left;
  background: #fff;
  border: 0;
  min-height: 30px;
  font-size: 12px;
  padding: 4px 6px;
}
.workspace-statusbar .header-more-menu > button:hover {
  background: #f6f8fb;
}
.header-more-menu > .header-stop {
  color: #be4c4c;
}
.header-more-menu p {
  font-size: 11px;
  line-height: 1.6;
  margin: 4px 0 0;
  color: #8797aa;
}
.header-feedback {
  position: absolute;
  right: 24px;
  top: 60px;
  display: flex;
  align-items: center;
  gap: 16px;
  max-width: calc(100vw - 32px);
  padding: 10px 14px;
  border: 1px solid #dce8e5;
  background: #f4faf8;
  box-shadow: 0 4px 15px #1e293b12;
  border-radius: 6px;
  font-size: 12px;
}
.workspace-statusbar .header-feedback button {
  border: 0;
  background: transparent;
  padding: 0;
  min-height: 20px;
  color: #8797aa;
}
@media (max-width: 1200px) {
  .workspace-statusbar,
  .header-controls {
    gap: 12px;
  }
  .header-readings {
    gap: 12px;
  }
  .header-user {
    gap: 8px;
    padding-left: 12px;
  }
  .workspace-statusbar .header-task {
    max-width: 150px;
  }
}
@media (max-width: 1140px) {
  .header-resource-wrap {
    flex-basis: 102px;
  }
  .resource-caption {
    font-size: 9px;
  }
}
@media (max-width: 1000px) and (min-width: 761px) {
  .header-user a {
    display: none;
  }
  .header-power {
    padding-left: 10px;
  }
  .header-controls {
    gap: 8px;
  }
  .header-metric {
    gap: 4px;
  }
  .header-instance select {
    max-width: 90px;
  }
}
@media (max-width: 930px) {
  .workspace-statusbar {
    height: auto;
    flex-wrap: wrap;
    row-gap: 7px;
    padding-block: 7px;
  }
  .header-resources {
    position: relative;
    flex: 1 0 100%;
    order: 1;
  }
  .header-resource-wrap {
    position: static;
    flex: 1 1 0;
  }
  .header-resource-details {
    left: 0;
    right: auto;
  }
  .disk-quota-details {
    right: 0;
    left: auto;
  }
}
@media (max-width: 760px) {
  .workspace-statusbar {
    height: auto;
    min-height: 88px;
    padding: 9px 16px;
    flex-wrap: wrap;
    gap: 7px 12px;
  }
  .header-controls {
    width: 100%;
    gap: 10px;
  }
  .header-task-wrap {
    flex: 1;
  }
  .workspace-statusbar .header-task {
    max-width: 100%;
    width: 100%;
    justify-content: space-between;
  }
  .header-instance select {
    max-width: 88px;
  }
  .header-power {
    gap: 6px;
    padding-left: 8px;
  }
  .header-readings {
    margin: 0;
    gap: 8px;
  }
  .header-user {
    margin-left: auto;
    padding-left: 8px;
    gap: 8px;
  }
  .header-user a {
    display: none;
  }
  .header-task-picker {
    position: fixed;
    top: 49px;
    left: 16px;
    right: 16px;
    width: auto;
  }
  .header-more-menu {
    position: fixed;
    top: 49px;
    right: 16px;
    left: auto;
  }
  .header-feedback {
    right: 16px;
    top: 94px;
  }
  .header-state {
    max-width: 40px;
    overflow: hidden;
  }
}
@media (max-width: 460px) {
  .workspace-statusbar {
    column-gap: 7px;
  }
  .workspace-statusbar .header-user button > span {
    display: none;
  }
}
</style>
