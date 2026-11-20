<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue';
import { RouterLink } from 'vue-router';
import { api } from '../api';
import { bootstrap } from '../bootstrap';
import { useNotifications } from '../stores/notifications';
import { useInstances } from '../stores/instances';
import { usePolling } from '../composables/polling';
import { useResource } from '../composables/resource';
import {
  countdown,
  type AffectedTask,
  type NoticeState,
  type Notification,
} from '../features/notifications/model';
import { videoStates, type Job, type Page } from '../features/jobs/contracts';
import AppIcon from './AppIcon.vue';
import TaskLog from './TaskLog.vue';
import DiskDiagnosis from './DiskDiagnosis.vue';
import { diskBytes } from '../features/disk/model';

const notices = useNotifications(),
  instances = useInstances();
const editable = bootstrap().role !== 'viewer';
const now = ref(Date.now() / 1000),
  closeButton = ref<HTMLButtonElement>(),
  drawer = ref<HTMLElement>();
const states: Array<{ id: NoticeState; label: string }> = [
  { id: 'action', label: '需处理' },
  { id: 'recovering', label: '自动恢复' },
  { id: 'resolved', label: '已解决' },
];
const jobs = useResource<Page<Job>>(),
  jobQuery = ref('');
const affected = ref<Notification>(),
  affectedTasks = useResource<Page<AffectedTask>>();
const log = ref<{ id: string; video_id?: string; instance_id?: string }>();
const busy = ref(''),
  feedback = ref(''),
  actionError = ref('');
const stale = computed(() => !!notices.sampledAt && now.value - notices.sampledAt > 35);
const resource = computed(() => notices.resourceAlerts[0]);
function showResource() {
  notices.state = 'action';
  notices.instance = resource.value?.instance_id || '';
  notices.job = '';
  notices.show();
}
let timer: ReturnType<typeof setInterval>, searchTimer: ReturnType<typeof setTimeout>;
let previousFocus: HTMLElement | null = null;
async function loadJobs(append = false) {
  const before = jobs.data.value?.items || [];
  const params = new URLSearchParams({ q: jobQuery.value, limit: '50' });
  if (append && jobs.data.value?.next_cursor) params.set('cursor', jobs.data.value.next_cursor);
  await jobs.load(async (signal) => {
    const page = await api.request<Page<Job>>('/api/jobs?' + params, { signal });
    return { ...page, items: append ? [...before, ...page.items] : page.items };
  });
}
watch(jobQuery, () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => void loadJobs(), 300);
});
watch(
  () => notices.open,
  async (opened) => {
    if (opened) {
      previousFocus = document.activeElement as HTMLElement;
      void loadJobs();
      await nextTick();
      closeButton.value?.focus();
    } else {
      affected.value = undefined;
      affectedTasks.clear();
      if (previousFocus?.isConnected) previousFocus.focus();
    }
  },
);
function changeFilter() {
  affected.value = undefined;
  affectedTasks.clear();
  notices.filter();
}
function showState(state: NoticeState) {
  notices.state = state;
  changeFilter();
}
async function loadAffected(item: Notification, append = false) {
  if (!append) affectedTasks.clear();
  affected.value = item;
  const before = affectedTasks.data.value?.items || [];
  const params = new URLSearchParams();
  if (append && affectedTasks.data.value?.next_cursor)
    params.set('cursor', affectedTasks.data.value.next_cursor);
  await affectedTasks.load(async (signal) => {
    const page = await api.request<Page<AffectedTask>>(
      '/api/notifications/' + item.id + '/tasks?' + params,
      { signal },
    );
    return { ...page, items: append ? [...before, ...page.items] : page.items };
  });
}
function openLog(item: Notification, taskId = item.detail.task_id, videoId = item.detail.video_id) {
  if (!taskId) return;
  log.value = {
    id: taskId,
    video_id: videoId,
    instance_id: item.instance_id === 'local' ? undefined : item.instance_id,
  };
}
async function retry(item: Notification, taskId = item.detail.task_id) {
  if (!editable || busy.value || !item.job_id || !taskId) return;
  busy.value = taskId;
  actionError.value = feedback.value = '';
  try {
    await api.request('/api/jobs/' + item.job_id + '/redeliver', {
      method: 'POST',
      body: { ids: [taskId], use_latest: false },
    });
    feedback.value = '已加入重试队列，仅获取缺失产物；任务未启动时请在顶部开启。';
    await notices.reload();
    if (affected.value) await loadAffected(affected.value);
  } catch (failure) {
    actionError.value = failure instanceof Error ? failure.message : String(failure);
  } finally {
    busy.value = '';
  }
}
function target(item: Notification) {
  const query = item.instance_id === 'local' ? {} : { instance_id: item.instance_id };
  if (item.code === 'disk' || item.code === 'disk_resource')
    return { path: '/settings', query, hash: '#maintenance' };
  if (item.code === 'account_auth' || item.code === 'rate_limit')
    return { path: '/accounts', query };
  if (item.code === 'instance') return { path: '/instances', query };
  return { path: item.job_id ? '/jobs/' + item.job_id : '/logs', query };
}
function targetLabel(item: Notification) {
  if (item.code === 'disk' || item.code === 'disk_resource') return '检查空间';
  if (item.code === 'account_auth' || item.code === 'rate_limit') return '检查账号';
  if (item.code === 'instance') return '查看实例';
  return item.job_id ? '查看任务' : '执行记录';
}
function time(value?: number | null) {
  return value ? new Date(value * 1000).toLocaleString('zh-CN', { hour12: false }) : '—';
}
function escape(event: KeyboardEvent) {
  if (event.key === 'Escape' && notices.open && !document.querySelector('dialog[open]'))
    notices.close();
}
function outside(event: PointerEvent) {
  if (!notices.open || document.querySelector('dialog[open], .el-message-box')) return;
  const target = event.target as Element;
  if (!drawer.value?.contains(target) && !target.closest('[aria-controls="notification-center"]'))
    notices.close();
}
onMounted(() => {
  timer = setInterval(() => {
    now.value = Date.now() / 1000;
  }, 1000);
  document.addEventListener('keydown', escape);
  document.addEventListener('pointerdown', outside);
});
onUnmounted(() => {
  clearInterval(timer);
  clearTimeout(searchTimer);
  document.removeEventListener('keydown', escape);
  document.removeEventListener('pointerdown', outside);
  notices.dispose();
});
usePolling(
  computed(() => !busy.value),
  (signal) => notices.refresh(signal),
  10000,
);
</script>

<template>
  <div
    v-if="notices.error || stale"
    class="global-notice-summary notification-unavailable"
    role="status"
  >
    <AppIcon name="alert" />
    <div>
      <strong>通知状态{{ notices.error ? '读取失败' : '待更新' }}</strong>
      <small
        >{{
          notices.error || '当前显示上次读取结果，暂不能确认是否有新问题。'
        }}
        已读取的问题仍会保留。</small
      >
    </div>
    <button @click="notices.show()">查看并重试</button>
  </div>
  <div v-else-if="resource" class="global-notice-summary disk-resource-summary" role="status">
    <span class="summary-icon"><AppIcon name="alert" /></span>
    <div>
      <strong>{{ resource.instance_name }} · {{ resource.detail.headline }}</strong>
      <small
        >{{ resource.detail.disk?.download?.impact_text }} · {{ resource.detail.reason
        }}<template v-if="resource.detail.affected_tasks">
          · {{ resource.detail.affected_tasks }} 个任务受影响</template
        ><template v-if="notices.resourceAlerts.length > 1">
          · 另有 {{ notices.resourceAlerts.length - 1 }} 个实例空间告警</template
        ></small
      >
    </div>
    <button @click="showResource">查看空间诊断 <AppIcon name="arrow" /></button>
  </div>
  <div v-else-if="notices.globalCounts.action" class="global-notice-summary" role="status">
    <span class="summary-icon"><AppIcon name="alert" /></span>
    <div>
      <strong>{{ notices.globalCounts.action }} 项问题需要处理</strong>
      <span v-if="notices.globalCounts.recovering" class="summary-recovery">
        · {{ notices.globalCounts.recovering }} 项正在自动恢复</span
      >
      <small>存在下载失败、交付缺项或执行条件受阻，请查看原因与受影响任务。</small>
    </div>
    <button @click="notices.show()">查看问题 <AppIcon name="arrow" /></button>
  </div>
  <Teleport to="body">
    <section
      v-if="notices.open"
      ref="drawer"
      id="notification-center"
      class="notification-drawer"
      role="dialog"
      aria-label="通知中心"
      aria-modal="false"
    >
      <header class="notification-heading">
        <div>
          <h2>通知中心</h2>
          <p>下载出了什么问题，在这里看清原因与处理方式。</p>
        </div>
        <button ref="closeButton" aria-label="关闭通知中心" @click="notices.close()">×</button>
      </header>
      <div class="notification-filters">
        <label
          ><span class="sr-only">通知实例范围</span
          ><select v-model="notices.instance" @change="changeFilter">
            <option value="">所有实例</option>
            <option v-if="!bootstrap().fleetEnabled" value="local">本机</option>
            <option v-for="item in instances.items" :key="item.id" :value="item.id">
              {{ item.name }}
            </option>
          </select></label
        >
        <label
          ><span class="sr-only">通知任务范围</span
          ><select v-model="notices.job" @change="changeFilter">
            <option value="">所有任务</option>
            <option
              v-if="notices.job && !jobs.data.value?.items.some((j) => j.id === notices.job)"
              :value="notices.job"
            >
              所选任务
            </option>
            <option v-for="item in jobs.data.value?.items" :key="item.id" :value="item.id">
              {{ item.customer }} / {{ item.name }}
            </option>
          </select></label
        >
        <details>
          <summary>搜索任务</summary>
          <input
            v-model="jobQuery"
            aria-label="搜索通知任务范围"
            placeholder="输入客户或任务名称"
          />
          <button
            v-if="jobs.data.value?.next_cursor"
            :disabled="jobs.loading.value"
            @click="loadJobs(true)"
          >
            加载更多任务
          </button>
        </details>
        <small>全局通知，不跟随顶部实例切换</small>
        <p v-if="jobs.error.value" class="form-error">任务筛选读取失败：{{ jobs.error.value }}</p>
      </div>
      <nav class="notification-tabs" aria-label="通知状态">
        <button
          v-for="item in states"
          :key="item.id"
          :class="{ selected: notices.state === item.id }"
          :aria-pressed="notices.state === item.id"
          @click="showState(item.id)"
        >
          {{ item.label }} <b>{{ notices.counts[item.id] }}</b>
        </button>
      </nav>
      <div class="notification-body">
        <p v-if="notices.error" class="form-error" role="alert">
          {{ notices.error }} <button @click="notices.reload()">重新读取</button>
        </p>
        <p v-if="actionError" class="form-error" role="alert">{{ actionError }}</p>
        <p v-if="feedback" class="notification-feedback" role="status">{{ feedback }}</p>
        <template v-if="affected"
          ><div class="notification-body-label">
            <button @click="affected = undefined">← 返回通知</button><span>受影响视频</span>
          </div>
          <p class="cell-note">
            {{ affected.instance_name }} / {{ affected.job_name }} · {{ affected.detail.headline }}
          </p>
          <p v-if="affectedTasks.error.value" class="form-error">
            {{ affectedTasks.error.value }}
            <button @click="loadAffected(affected)">重试读取</button>
          </p>
          <article
            v-for="task in affectedTasks.data.value?.items"
            :key="task.task_id"
            class="notification-card affected-task"
          >
            <div class="notification-card-content">
              <strong>{{ task.detail.title || task.detail.video_id }}</strong>
              <p>{{ videoStates[task.status] || task.status }} · {{ task.detail.phase }}</p>
              <p>{{ task.detail.reason }}</p>
              <p v-if="task.detail.disk_pressure">
                申请新增预留 {{ diskBytes(task.detail.disk_pressure.requested_bytes) }} · 当时可分配
                {{ diskBytes(task.detail.disk_pressure.allocatable_bytes) }}
              </p>
              <p v-if="task.detail.retry_at" class="notification-countdown">
                {{ countdown(task.detail.retry_at, now) }}
              </p>
              <p v-if="task.detail.missing.length" class="notification-missing">
                未完成：{{ task.detail.missing.join('、') }}
              </p>
            </div>
            <div class="notification-card-actions">
              <button
                v-if="editable && affected.job_id && task.status !== 'running'"
                :disabled="!!busy"
                @click="retry(affected, task.task_id)"
              >
                重试该视频
              </button>
              <button @click="openLog(affected, task.task_id, task.detail.video_id)">
                执行日志 ↗
              </button>
            </div>
          </article>
          <button
            v-if="affectedTasks.data.value?.next_cursor"
            :disabled="affectedTasks.loading.value"
            @click="loadAffected(affected, true)"
          >
            加载更多受影响视频
          </button>
          <p
            v-if="!affectedTasks.loading.value && !affectedTasks.data.value?.items.length"
            class="notification-empty"
          >
            本组已没有未解决的视频，或这是实例资源通知；请查看任务与实例状态。
          </p>
        </template>
        <template v-else>
          <div class="notification-body-label">
            <span>{{
              notices.state === 'resolved' ? '保留最近一次问题与恢复时间' : '未解决问题持续保留'
            }}</span
            ><span>查看不等于解决</span>
          </div>
          <article
            v-for="item in notices.items"
            :key="item.id"
            class="notification-card"
            :class="item.status"
          >
            <div class="notification-card-content">
              <div class="notification-title">
                <span class="notification-icon"
                  ><AppIcon
                    :name="
                      item.status === 'resolved'
                        ? 'check'
                        : item.status === 'recovering'
                          ? 'clock'
                          : 'alert'
                    "
                /></span>
                <h3>{{ item.detail.headline }}</h3>
                <b v-if="item.detail.prolonged" class="prolonged">持续受阻</b>
              </div>
              <div class="notification-context">
                {{ item.instance_name
                }}<template v-if="item.job_name">
                  / {{ item.customer }} · {{ item.job_name }}</template
                ><template v-if="item.detail.video_id"> / {{ item.detail.video_id }}</template>
              </div>
              <p v-if="item.detail.title" class="notification-video">{{ item.detail.title }}</p>
              <div
                v-if="item.detail.completed.length || item.detail.missing.length"
                class="notification-deliverables"
              >
                <span v-for="name in item.detail.completed" :key="'done:' + name" class="complete"
                  >✓ {{ name }}</span
                ><span v-for="name in item.detail.missing" :key="'missing:' + name" class="missing"
                  >! {{ name }}{{ item.status === 'resolved' ? '（当时缺失）' : '待完成' }}</span
                >
              </div>
              <dl v-if="!item.detail.disk?.download">
                <div>
                  <dt>发生环节：</dt>
                  <dd>
                    {{ item.detail.phase
                    }}<b v-if="item.detail.http_status" class="http-code">
                      HTTP {{ item.detail.http_status }}</b
                    >
                  </dd>
                </div>
                <div>
                  <dt>原因：</dt>
                  <dd>{{ item.detail.reason }}</dd>
                </div>
                <div v-if="item.status !== 'resolved'">
                  <dt>下一步：</dt>
                  <dd>{{ item.detail.next_step }}</dd>
                </div>
              </dl>
              <DiskDiagnosis
                v-if="item.detail.disk?.download"
                :diagnostic="item.detail.disk"
                :historical="item.status === 'resolved'"
              />
              <p v-if="item.detail.recovery" class="notification-activity">
                本次告警期间可用空间增加 {{ diskBytes(item.detail.recovery.freed_bytes) }}。
              </p>
              <p v-if="item.detail.activity" class="notification-activity">
                {{ item.detail.activity }}
              </p>
              <p
                v-if="item.detail.retry_at && item.status !== 'resolved'"
                class="notification-countdown"
              >
                {{ countdown(item.detail.retry_at, now) }}<small> · 到期仍需满足执行条件</small>
              </p>
              <details v-if="item.detail.raw" class="notification-raw">
                <summary>
                  原始信息<span v-if="item.detail.max_attempts">
                    · 已尝试 {{ item.detail.attempts }} / {{ item.detail.max_attempts }} 次</span
                  >
                </summary>
                <pre>{{ item.detail.raw }}</pre>
              </details>
              <small class="notification-time"
                >首次发现 {{ time(item.first_seen)
                }}<template v-if="item.status === 'resolved'">
                  · 已结束 {{ time(item.resolved_at) }}</template
                ><template v-else> · 更新 {{ time(item.updated_at) }}</template></small
              >
            </div>
            <div class="notification-card-actions">
              <a
                v-if="item.code === 'disk' || item.code === 'disk_resource'"
                class="notification-primary"
                :href="
                  '/settings?instance_id=' +
                  encodeURIComponent(item.instance_id || '') +
                  '#maintenance'
                "
                @click="notices.close()"
                >前往空间与日志处理</a
              >
              <RouterLink
                v-else
                class="notification-primary"
                :to="target(item)"
                @click="notices.close()"
                >{{ targetLabel(item) }}</RouterLink
              >
              <button
                v-if="
                  editable &&
                  item.job_id &&
                  item.detail.task_id &&
                  item.status !== 'resolved' &&
                  item.detail.task_status !== 'running'
                "
                :disabled="!!busy"
                @click="retry(item)"
              >
                {{ busy === item.detail.task_id ? '提交中…' : '重试该视频' }}
              </button>
              <button
                v-if="item.detail.task_id"
                class="notification-text-button"
                @click="openLog(item)"
              >
                执行日志 ↗
              </button>
              <button
                v-if="item.active_count > 1 || item.code === 'disk_resource'"
                class="notification-text-button notification-group-link"
                @click="loadAffected(item)"
              >
                {{
                  item.code === 'disk_resource'
                    ? '查看已等待任务 ↗'
                    : item.active_count + ' 个视频 · 查看全部 ↗'
                }}
              </button>
              <span v-else-if="item.detail.affected_tasks" class="notification-impact"
                >影响 {{ item.detail.affected_tasks }} 个待执行视频</span
              >
            </div>
          </article>
          <p v-if="notices.loading && !notices.items.length" class="notification-empty">
            正在读取问题记录…
          </p>
          <p v-else-if="!notices.items.length && !notices.error" class="notification-empty">
            {{
              notices.state === 'action'
                ? '当前范围没有需要处理的问题。'
                : notices.state === 'recovering'
                  ? '当前范围没有正在自动恢复的问题。'
                  : '当前范围暂无已解决记录。'
            }}
          </p>
          <button
            v-if="notices.nextCursor"
            class="notification-load"
            :disabled="notices.loading"
            @click="notices.refresh(undefined, true)"
          >
            加载更多通知
          </button>
          <button
            v-if="notices.state === 'action' && notices.globalCounts.recovering"
            class="notification-auto-summary"
            @click="showState('recovering')"
          >
            <AppIcon name="clock" />
            {{ notices.globalCounts.recovering }} 项正在自动恢复，查看进度与倒计时 →
          </button>
        </template>
      </div>
      <footer class="notification-footer">
        <span>●</span> 恢复或接受来源缺失后自动归档，保留最近记录<button
          :disabled="notices.loading"
          @click="changeFilter()"
        >
          刷新
        </button>
      </footer>
    </section>
    <TaskLog v-if="log" :key="log.id" :task="log" @close="log = undefined" />
  </Teleport>
</template>

<style scoped>
.sr-only {
  position: absolute;
  width: 1px;
  height: 1px;
  overflow: hidden;
  clip-path: inset(50%);
}
.global-notice-summary {
  display: flex;
  align-items: center;
  gap: 12px;
  margin: 18px 28px 0;
  padding: 12px 15px;
  border: 1px solid #efd9b3;
  background: #fff9ee;
  border-radius: 8px;
  color: #92621f;
  font-size: 12px;
}
.global-notice-summary small {
  display: block;
  color: #a58b68;
  font-size: 11px;
  margin-top: 3px;
}
.summary-icon {
  padding: 6px;
  border-radius: 6px;
  background: #fcedce;
}
.global-notice-summary > button {
  margin-left: auto;
  flex-shrink: 0;
  color: #946222;
  background: #fffcf6;
  border-color: #e7cca3;
  font-size: 11px;
}
.summary-recovery {
  color: #b18e5b;
}
.notification-unavailable {
  color: #b75346;
  background: #fff5f3;
  border-color: #f0d4cc;
}
.notification-drawer {
  position: fixed;
  top: 54px;
  right: 0;
  bottom: 0;
  width: min(500px, 100vw);
  z-index: 24;
  display: flex;
  flex-direction: column;
  background: #fff;
  border-left: 1px solid #dce4ef;
  box-shadow: -12px 0 32px #1d34551a;
  color: #33435a;
  font-size: 12px;
}
.notification-heading {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  padding: 22px 22px 12px;
}
.notification-heading h2 {
  font-size: 19px;
  margin: 0 0 6px;
}
.notification-heading p {
  font-size: 11px;
  color: #8492a7;
  margin: 0;
}
.notification-drawer .notification-heading > button {
  font-size: 22px;
  border: 0;
  background: transparent;
  color: #8090a6;
  padding: 0 4px;
  min-height: 27px;
}
.notification-filters {
  padding: 0 22px 15px;
  display: flex;
  gap: 8px;
  align-items: center;
  flex-wrap: wrap;
}
.notification-filters label {
  flex: 1;
  min-width: 0;
}
.notification-filters select {
  font: inherit;
  min-width: 0;
  width: 100%;
  padding: 6px;
  background: #f6f8fb;
  border: 1px solid #e1e7ef;
  border-radius: 5px;
  color: #60738b;
}
.notification-filters small {
  color: #92a0b2;
  font-size: 10px;
  flex-basis: 100%;
}
.notification-filters details {
  color: #7790b3;
  font-size: 11px;
}
.notification-filters details[open] {
  flex-basis: 100%;
}
.notification-filters details input {
  width: 100%;
  margin-top: 6px;
}
.notification-tabs {
  display: flex;
  gap: 22px;
  padding: 0 22px;
  border-bottom: 1px solid #e8edf3;
}
.notification-drawer .notification-tabs > button {
  border: 0;
  border-radius: 0;
  padding: 8px 0 12px;
  background: transparent;
  color: #8591a5;
  font-size: 12px;
}
.notification-drawer .notification-tabs > button.selected {
  color: #2563eb;
  box-shadow: inset 0 -2px #2563eb;
}
.notification-tabs b {
  font-size: 10px;
  background: #f0f3f7;
  padding: 1px 5px;
  margin-left: 4px;
  border-radius: 4px;
}
.notification-tabs .selected b {
  background: #edf3ff;
}
.notification-body {
  overflow-y: auto;
  overscroll-behavior: contain;
  flex: 1;
  padding: 17px 19px;
  min-height: 0;
}
.notification-body-label {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin: 0 2px 13px;
  color: #8a98ab;
  font-size: 11px;
}
.notification-card {
  border: 1px solid #e0e6ef;
  border-radius: 8px;
  margin-bottom: 13px;
  overflow: hidden;
  background: #fff;
}
.notification-card-content {
  padding: 14px 15px;
}
.notification-title {
  display: flex;
  align-items: center;
  gap: 8px;
}
.notification-title h3 {
  font-size: 13px;
  line-height: 1.6;
  margin: 0;
  flex: 1;
  font-weight: 600;
}
.notification-icon {
  width: 27px;
  height: 27px;
  border-radius: 6px;
  display: flex;
  align-items: center;
  justify-content: center;
  background: #fff2e1;
  color: #b97a2a;
}
.notification-card.recovering .notification-icon {
  background: #edf3ff;
  color: #6082b2;
}
.notification-card.resolved .notification-icon {
  background: #ecf8f2;
  color: #3c987c;
}
.notification-context {
  font-size: 10px;
  color: #8b9aaf;
  margin: 8px 0;
  overflow-wrap: anywhere;
}
.notification-video {
  margin: 0 0 8px;
  font-size: 12px;
  overflow-wrap: anywhere;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
.notification-deliverables {
  display: flex;
  flex-wrap: wrap;
  gap: 5px;
  margin: 9px 0;
}
.notification-deliverables span {
  padding: 2px 6px;
  border-radius: 4px;
  font-size: 10px;
}
.notification-deliverables .complete {
  background: #f0f7f4;
  color: #688c7c;
}
.notification-deliverables .missing {
  background: #fff2e5;
  color: #b37631;
}
.notification-card dl {
  font-size: 11px;
  line-height: 1.85;
  margin: 9px 0;
  color: #718198;
}
.notification-card dl > div {
  display: flex;
  align-items: baseline;
}
.notification-card dt {
  flex-shrink: 0;
  color: #566b86;
}
.notification-card dd {
  margin: 0;
  overflow-wrap: anywhere;
}
.http-code {
  font-size: 10px;
  color: #bf6960;
  font-weight: 500;
}
.notification-time {
  display: block;
  font-size: 10px;
  color: #96a3b5;
  margin-top: 10px;
}
.notification-card-actions {
  display: flex;
  align-items: center;
  gap: 7px;
  padding: 10px 14px;
  border-top: 1px solid #eef1f6;
  background: #fafbfd;
  flex-wrap: wrap;
}
.notification-card-actions button {
  min-height: 28px;
  font-size: 11px;
  padding: 4px 8px;
}
.notification-primary {
  display: inline-flex;
  padding: 5px 9px;
  background: #2563eb;
  color: white;
  border-radius: 5px;
  text-decoration: none;
  font-size: 11px;
  min-height: 28px;
  align-items: center;
}
.notification-card-actions .notification-text-button {
  border: 0;
  background: transparent;
  padding-left: 2px;
  color: #7488a5;
}
.notification-group-link {
  margin-left: auto;
}
.notification-impact {
  font-size: 10px;
  color: #899bb0;
}
.notification-drawer details.notification-raw {
  border: 0;
  border-radius: 0;
  background: transparent;
  padding: 0;
  margin: 9px 0 0;
}
.notification-drawer .notification-filters details {
  border: 0;
  padding: 0;
  margin: 0;
  background: transparent;
}
.notification-drawer .notification-filters summary {
  padding: 0;
}
.notification-drawer .notification-raw summary {
  padding: 0;
  background: transparent;
  font-size: 10px;
  cursor: pointer;
  color: #91a0b3;
}
.notification-raw pre {
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  max-height: 200px;
  overflow: auto;
  background: #f5f7fa;
  padding: 8px;
  font-size: 11px;
  color: #7d6c64;
}
.notification-countdown,
.notification-activity {
  font-size: 11px;
  color: #527fbe;
  margin: 8px 0;
}
.notification-countdown small {
  color: #97a3b5;
  font-size: 10px;
}
.prolonged {
  font-size: 10px;
  color: #b8782f;
  background: #fff3e3;
  padding: 2px 5px;
  border-radius: 4px;
}
.notification-auto-summary {
  width: 100%;
  text-align: left;
  padding: 13px !important;
  background: #f8fafd !important;
  color: #7289aa;
  font-size: 11px !important;
  margin: 4px 0 10px;
}
.notification-auto-summary .app-icon {
  margin-right: 6px;
}
.notification-footer {
  display: flex;
  gap: 6px;
  align-items: center;
  font-size: 10px;
  color: #8d9baf;
  padding: 12px 20px;
  border-top: 1px solid #e8edf4;
}
.notification-footer > span {
  color: #49a28a;
  font-size: 9px;
}
.notification-drawer .notification-footer > button {
  margin-left: auto;
  min-height: 25px;
  padding: 3px 7px;
  font-size: 10px;
}
.notification-empty {
  text-align: center;
  color: #91a0b3;
  padding: 45px 15px;
  line-height: 1.9;
}
.notification-load {
  width: 100%;
}
.notification-feedback {
  background: #edf8f3;
  color: #458770;
  border: 1px solid #cde9dc;
  border-radius: 6px;
  padding: 10px;
  font-size: 11px;
}
.notification-missing {
  color: #aa7c47;
}
.affected-task p {
  font-size: 11px;
  color: #7a8da5;
  line-height: 1.8;
  margin: 6px 0;
}
.affected-task strong {
  font-size: 12px;
  overflow-wrap: anywhere;
}
@media (max-width: 700px) {
  .notification-drawer {
    top: 0;
    width: 100%;
    z-index: 35;
  }
  .global-notice-summary {
    margin: 12px 12px 0;
    align-items: flex-start;
    font-size: 11px;
    gap: 8px;
  }
  .global-notice-summary small {
    font-size: 10px;
  }
  .summary-recovery {
    display: block;
  }
  .global-notice-summary > button {
    font-size: 10px;
    padding: 5px;
  }
  .notification-filters {
    padding-right: 18px;
    padding-left: 18px;
  }
  .notification-body {
    padding: 15px;
  }
  .notification-footer {
    font-size: 9px;
  }
}
</style>
