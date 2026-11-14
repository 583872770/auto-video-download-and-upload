<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue';
import { RouterLink, useRoute, useRouter } from 'vue-router';
import { api } from '../../api';
import { bootstrap } from '../../bootstrap';
import { useResource } from '../../composables/resource';
import { usePolling } from '../../composables/polling';
import { confirmNavigation, useLeaveGuard } from '../../composables/leaveGuard';
import JobForm from './JobForm.vue';
import JobVideos from './JobVideos.vue';
import AppendVideos from './AppendVideos.vue';
import TaskConfirmation from './TaskConfirmation.vue';
import SystemNotice from '../../components/SystemNotice.vue';
import { outputLabels } from '../delivery/model';
import {
  amount,
  formatGB,
  jobEndpoint,
  jobStates,
  videoStates,
  type Job,
  type JobStatistics,
} from './contracts';
const route = useRoute(),
  router = useRouter();
const id = computed(() => String(route.params.id));
const tabs = {
  overview: '任务概览',
  videos: '视频清单',
  billing: '交付与账单',
  history: '操作记录',
  settings: '任务设置',
};
type Tab = keyof typeof tabs;
const tab = computed<Tab>(() =>
  typeof route.query.tab === 'string' && route.query.tab in tabs
    ? (route.query.tab as Tab)
    : 'overview',
);
const resource = useResource<Job>(),
  statistics = useResource<JobStatistics>();
const job = resource.data,
  editable = bootstrap().role !== 'viewer';
const adding = ref(false),
  confirmation = ref<'delete' | undefined>(),
  busy = ref(false),
  message = ref(''),
  error = ref(''),
  editorRevision = ref(0);
useLeaveGuard(() => ({ dirty: false, busy: busy.value }));
async function refresh(signal?: AbortSignal) {
  await resource.load((signal) => api.request(jobEndpoint(id.value), { signal }), signal);
  if (tab.value === 'history' || tab.value === 'billing')
    await statistics.load(
      (signal) => api.request(jobEndpoint(id.value) + '/statistics', { signal }),
      signal,
    );
}
usePolling(
  computed(
    () =>
      !adding.value &&
      !confirmation.value &&
      !busy.value &&
      ['overview', 'history', 'billing'].includes(tab.value),
  ),
  refresh,
  5000,
);
watch(
  tab,
  () => {
    void refresh();
  },
  { immediate: true },
);
async function switchTab(value: Tab) {
  await router.replace({ query: { ...route.query, tab: value } });
}
async function add() {
  await switchTab('videos');
  if (tab.value === 'videos') adding.value = true;
}
async function saved() {
  message.value = '任务信息和整单预计费用已更新。';
  await refresh();
  editorRevision.value++;
}
async function control(action: string) {
  if (busy.value || !editable || !(await confirmNavigation())) return;
  busy.value = true;
  error.value = '';
  try {
    const result = await api.request<{ warnings?: string[] }>(jobEndpoint(id.value) + '/control', {
      method: 'POST',
      body: { action },
    });
    message.value =
      result.warnings?.join('；') ||
      {
        archive: '任务已归档；账单和已下载文件保留。',
        unarchive: '任务已恢复，请前往下载工作台选择后开始。',
        pause: '此任务将完成当前下载后暂停。',
        stop: '此任务已停止，执行进程将在下次心跳退出。',
      }[action] ||
      '任务操作已保存。';
    await refresh();
    editorRevision.value++;
  } catch (failure) {
    error.value = String(failure);
  } finally {
    busy.value = false;
  }
}
async function remove() {
  if (await confirmNavigation()) confirmation.value = 'delete';
}
async function deleted() {
  confirmation.value = undefined;
  job.value = undefined;
  await nextTick();
  await router.push('/jobs');
}
function samples(value: string) {
  try {
    return (JSON.parse(value) as string[]).join('；');
  } catch {
    return '';
  }
}
</script>
<template>
  <main class="content" id="detail-view">
    <RouterLink id="back" class="text-link" to="/jobs">← 返回任务中心</RouterLink>
    <SystemNotice />
    <p v-if="resource.error.value" role="alert" class="form-error">
      {{ resource.error.value }} <button @click="refresh()">重新读取</button>
    </p>
    <p v-if="!job" role="status">
      {{ resource.error.value ? '任务暂不可读取。' : '正在读取任务…' }}
    </p>
    <template v-if="job">
      <div id="detail-heading" class="page-heading job-heading">
        <div>
          <p>
            {{ job.customer }} <span class="badge">{{ jobStates[job.state] || job.state }}</span>
          </p>
          <h1>{{ job.name }}</h1>
          <p id="task-context">
            任务编号 {{ job.id }}<template v-if="job.note"> · {{ job.note }}</template>
          </p>
        </div>
        <div class="actions">
          <button
            class="primary"
            data-action="append"
            :disabled="!editable || job.archived || busy"
            @click="add"
          >
            ＋ 添加视频</button
          ><RouterLink
            class="primary text-link"
            :to="{ path: '/workbench', query: { job: job.id } }"
            >前往下载 ↗</RouterLink
          >
        </div>
      </div>
      <p v-if="message" id="toast" role="status" class="notice success">{{ message }}</p>
      <p v-if="error" role="alert" class="form-error">{{ error }}</p>
      <p v-if="job.waiting_reason" class="notice">{{ job.waiting_reason }}</p>
      <nav class="tabbar" aria-label="任务详情分类">
        <button
          v-for="(label, key) in tabs"
          :key="key"
          :data-tab="key"
          :class="{ active: tab === key }"
          :aria-current="tab === key ? 'page' : undefined"
          @click="switchTab(key)"
        >
          {{ label }}
        </button>
      </nav>
      <div id="detail-content" class="page-stack">
        <section v-if="job.delivery.contract && ['overview', 'billing'].includes(tab)" class="card">
          <h2>交付要求 · 版本 {{ job.delivery.contract.version }}</h2>
          <div class="delivery-tags">
            <span v-for="kind in job.delivery.contract.spec.outputs" :key="kind" class="badge">{{
              outputLabels[kind]
            }}</span>
          </div>
          <p>
            全部交付 {{ job.delivery.states.complete || 0 }} 条 · 部分交付
            {{ job.delivery.states.partial || 0 }} 条 · 待确认
            {{ job.delivery.states.review || 0 }} 条 · 已按约定结束
            {{ job.delivery.states.accepted || 0 }} 条
          </p>
          <div v-for="(value, kind) in job.delivery.types" :key="kind" class="detail-row">
            <span>{{ outputLabels[kind] }}</span
            ><span>{{ value?.file_count }} 个文件 · {{ formatGB(value?.file_bytes) }}</span>
          </div>
          <div class="toolbar">
            <a class="text-link" :href="jobEndpoint(job.id) + '/artifacts/export'"
              >导出分类交付清单 CSV</a
            ><RouterLink class="text-link" :to="{ name: 'delivery', query: { job_id: job.id } }"
              >预览本任务交付文件 →</RouterLink
            >
          </div>
        </section>
        <template v-if="tab === 'overview' || tab === 'billing'">
          <div class="summary-grid">
            <div class="card">
              <small>当前有效视频</small><strong>{{ job.total }}</strong
              ><small>累计移除 {{ job.removed }} 个条目</small>
            </div>
            <div class="card">
              <small>处理完成率</small
              ><strong>{{
                job.progress_percent === null ? '—' : job.progress_percent + '%'
              }}</strong
              ><small>处理成功 {{ job.succeeded }} · 最终失败 {{ job.failed }}</small>
            </div>
            <div class="card">
              <small>累计交付</small><strong>{{ formatGB(job.file_bytes_exact) }}</strong
              ><small>{{ job.file_count }} 个文件 · 历史累计</small>
            </div>
            <div class="card">
              <small>本任务预计费用</small><strong>{{ amount(job) }}</strong
              ><small>{{
                job.price_set ? job.price + ' 元 / ' + job.unit : '请在任务设置中填写单价'
              }}</small>
            </div>
          </div>
        </template>
        <div v-if="tab === 'overview'" class="detail-grid">
          <section class="card">
            <div class="section-heading">
              <h2>视频处理进度</h2>
              <button @click="switchTab('videos')">管理视频 →</button>
            </div>
            <progress
              :value="job.progress_percent || 0"
              max="100"
              aria-label="处理完成率"
            ></progress>
            <div class="detail-row">
              <span>待处理 {{ job.queued }}</span
              ><span>执行中 {{ job.running }}</span>
            </div>
            <div class="detail-row">
              <span>处理成功 {{ job.succeeded }}</span
              ><span>最终失败 {{ job.failed }}</span>
            </div>
            <p>处理完成率包含最终失败。“执行已完成”与“交付已齐全”分别判断。</p>
          </section>
          <section class="card">
            <h2>当前执行</h2>
            <p>{{ (job.transfer_speed / 1e6).toFixed(2) }} MB/s</p>
            <div v-for="video in job.running_videos" :key="video.id" class="detail-row">
              <span
                >{{ video.video_id
                }}<small class="cell-note"
                  >{{ video.progress?.stage || '准备中' }} ·
                  {{ video.instance_id || '本机' }}</small
                ></span
              ><span>{{
                video.progress?.percent == null ? '—' : video.progress.percent + '%'
              }}</span>
            </div>
            <p v-if="!job.running_videos.length">
              当前没有正在执行的视频。在下载工作台选择此任务后开始。
            </p>
            <div class="actions">
              <button :disabled="!editable || job.archived || busy" @click="control('pause')">
                完成当前后暂停</button
              ><button
                class="danger"
                :disabled="!editable || job.archived || busy"
                @click="control('stop')"
              >
                立即停止此任务
              </button>
            </div>
          </section>
          <section class="card">
            <h2>交付与计费</h2>
            <div class="detail-row">
              <span>交付成品</span
              ><span>{{ job.file_count }} 个 · {{ formatGB(job.file_bytes_exact) }}</span>
            </div>
            <div class="detail-row">
              <span>测试成功 · 不计费</span
              ><span>{{ job.test_succeeded }} 个 · {{ formatGB(job.test_file_bytes_exact) }}</span>
            </div>
            <div class="detail-row">
              <span>历史跳过</span><span>{{ job.skipped }} 个视频</span>
            </div>
          </section>
          <section class="card">
            <h2>导入记录</h2>
            <div v-for="record in job.imports" :key="record.id" class="detail-row">
              <span
                >{{ record.name }} ·
                {{ record.finished < 0 ? '已取消' : record.finished ? '已完成' : '导入中'
                }}<small v-if="record.invalid" class="cell-note">{{
                  samples(record.samples)
                }}</small></span
              ><span
                >新增 {{ record.added }} · 重复 {{ record.duplicates }} · 无效
                {{ record.invalid }}</span
              >
            </div>
            <p v-if="!job.imports.length">尚无文件导入记录，可通过“添加视频”提交 TXT / CSV。</p>
          </section>
        </div>
        <JobVideos
          v-if="tab === 'videos'"
          :job="job"
          :initial-status="typeof route.query.status === 'string' ? route.query.status : undefined"
          @changed="refresh()"
          @message="message = $event"
        />
        <template v-if="tab === 'billing'"
          ><section class="card">
            <h2>任务账单与导出</h2>
            <p class="notice">
              按实际交付文件字节累计，暂停、重试及跨实例接管不另起账单。当前金额是预计费用。
            </p>
            <p>
              {{ job.file_bytes_exact }} 字节正式交付 ·
              {{ job.test_file_bytes_exact }} 字节测试（不计费）
            </p>
            <div class="toolbar">
              <a class="text-link" :href="jobEndpoint(job.id) + '/export?kind=bill'"
                >导出任务账单 CSV</a
              ><a class="text-link" :href="'/api/statistics/lists/' + job.id + '/files/export'"
                >导出交付明细 CSV</a
              ><a
                class="text-link"
                :href="'/api/statistics/lists/' + job.id + '/files/export?kind=test'"
                >导出测试明细 CSV</a
              >
            </div>
          </section>
          <section class="card">
            <h2>下载器执行统计</h2>
            <div
              v-for="provider in statistics.data.value?.providers"
              :key="provider.method + provider.status"
              class="detail-row"
            >
              <span
                >{{ provider.method }} · {{ videoStates[provider.status] || provider.status }}</span
              ><span>{{ provider.count }} 条执行记录</span>
            </div>
            <p v-if="!statistics.data.value?.providers.length">尚无执行记录。</p>
          </section>
          <div class="detail-grid">
            <section v-for="kind in ['files', 'test_files'] as const" :key="kind" class="card">
              <h2>{{ kind === 'files' ? '最近交付' : '最近测试' }}（最多 50 条）</h2>
              <div
                v-for="(file, index) in statistics.data.value?.[kind]"
                :key="index"
                class="detail-row"
              >
                <span>{{ file.title || file.video_id }}</span
                ><span>{{ formatGB(file.size) }}</span>
              </div>
              <p v-if="!statistics.data.value?.[kind].length">暂无文件登记。</p>
            </section>
          </div></template
        >
        <section v-if="tab === 'history'" class="card">
          <h2>任务操作记录（最近 50 条）</h2>
          <div v-for="event in statistics.data.value?.events" :key="event.seq" class="detail-row">
            <span>{{ event.message }}</span
            ><small>{{ event.created_at }}</small>
          </div>
          <p v-if="!statistics.data.value?.events.length">暂无操作记录。</p>
          <h3>导入记录</h3>
          <div v-for="record in job.imports" :key="record.id" class="detail-row">
            <span>{{ record.name }}</span
            ><span
              >新增 {{ record.added }} · 重复 {{ record.duplicates }} · 无效
              {{ record.invalid }}</span
            >
          </div>
        </section>
        <p
          v-if="['billing', 'history'].includes(tab) && statistics.error.value"
          role="alert"
          class="form-error"
        >
          {{ statistics.error.value }}
        </p>
        <template v-if="tab === 'settings'"
          ><section class="card">
            <h2>任务信息与计费</h2>
            <JobForm
              :key="editorRevision"
              :job="job"
              :disabled="!editable || job.archived || busy"
              @saved="saved"
            />
          </section>
          <details :key="editorRevision" class="card task-maintenance">
            <summary>归档与删除</summary>
            <p>归档会停止此任务并取消未完成导入。删除保留历史账单和已下载文件。</p>
            <div class="actions">
              <button
                :data-action="job.archived ? 'unarchive' : 'archive'"
                :disabled="!editable || busy"
                @click="control(job.archived ? 'unarchive' : 'archive')"
              >
                {{ job.archived ? '恢复归档任务' : '归档此任务' }}</button
              ><button
                class="danger"
                data-action="delete"
                :disabled="!editable || busy"
                @click="remove"
              >
                删除此任务
              </button>
            </div>
          </details></template
        >
      </div>
      <AppendVideos
        v-if="adding"
        :job="job"
        @close="adding = false"
        @completed="
          adding = false;
          message = $event;
          refresh();
        "
      />
      <TaskConfirmation
        v-if="confirmation"
        :job="job"
        :action="confirmation"
        @close="confirmation = undefined"
        @completed="deleted"
      />
    </template>
  </main>
</template>
