<script setup lang="ts">
import AppIcon from '../../components/AppIcon.vue';
import { computed, ref, watch } from 'vue';
import { RouterLink, useRouter } from 'vue-router';
import { storeToRefs } from 'pinia';
import { useJobList } from '../../stores/jobList';
import { api } from '../../api';
import { bootstrap } from '../../bootstrap';
import { usePolling } from '../../composables/polling';
import { useResource } from '../../composables/resource';
import { confirmNavigation } from '../../composables/leaveGuard';
import WorkspaceDialog from '../../components/WorkspaceDialog.vue';
import SystemNotice from '../../components/SystemNotice.vue';
import JobForm from './JobForm.vue';
import TaskConfirmation from './TaskConfirmation.vue';
import { formatGB, amount, jobStates, type Job, type Page, type JobSummary } from './contracts';
import { outputLabels } from '../delivery/model';
const router = useRouter(),
  editable = bootstrap().role !== 'viewer';
const { query, lifecycle, cursor, history } = storeToRefs(useJobList());
const list = useResource<Page<Job>>(),
  summary = useResource<JobSummary>();
const creating = ref(false),
  formBusy = ref(false),
  deleting = ref<Job>(),
  message = ref('');
async function refresh(signal?: AbortSignal) {
  const params = new URLSearchParams({ q: query.value, lifecycle: lifecycle.value });
  if (cursor.value) params.set('cursor', cursor.value);
  await list.load((signal) => api.request('/api/jobs?' + params, { signal }), signal);
}
usePolling(
  computed(() => !creating.value && !deleting.value),
  async (signal) => {
    await Promise.all([
      refresh(signal),
      summary.load((signal) => api.request('/api/jobs/summary', { signal }), signal),
    ]);
  },
  5000,
);
let searchTimer: ReturnType<typeof setTimeout> | undefined;
import { onUnmounted } from 'vue';
watch(query, () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => {
    cursor.value = null;
    history.value = [];
    void refresh();
  }, 300);
});
onUnmounted(() => clearTimeout(searchTimer));
watch(lifecycle, () => {
  cursor.value = null;
  history.value = [];
  void refresh();
});
function page(next: boolean) {
  if (next && list.data.value?.next_cursor) {
    history.value.push(cursor.value);
    cursor.value = list.data.value.next_cursor;
  } else if (!next) cursor.value = history.value.pop() || null;
  void refresh();
}
async function created(id: string) {
  creating.value = false;
  await router.push({ name: 'job', params: { id }, query: { tab: 'videos' } });
}
async function deleted() {
  deleting.value = undefined;
  message.value = '任务已删除；历史账单和已下载文件保留。';
  cursor.value = null;
  history.value = [];
  await refresh();
}
</script>
<template>
  <main class="content" id="center-view">
    <div class="page-heading">
      <div>
        <span class="eyebrow">CUSTOMER JOBS</span>
        <h1>任务中心</h1>
        <p>管理客户任务、视频清单、交付要求和预计费用。</p>
      </div>
      <div class="actions">
        <RouterLink class="text-link" to="/workbench">下载工作台 ↗</RouterLink
        ><button id="new-task" class="primary" :disabled="!editable" @click="creating = true">
          <AppIcon name="plus" />新建任务
        </button>
      </div>
    </div>
    <p v-if="!editable" class="notice">当前角色只有查看权限。</p>
    <SystemNotice />
    <p v-if="message" role="status">{{ message }}</p>
    <div class="summary-grid" id="center-cards">
      <div class="card">
        <small>有效任务</small><strong>{{ summary.data.value?.total ?? '—' }}</strong
        ><small>未归档的客户任务</small>
      </div>
      <div class="card">
        <small>执行中任务</small><strong>{{ summary.data.value?.running ?? '—' }}</strong
        ><small>按实例独立执行</small>
      </div>
      <div class="card">
        <small>累计交付</small
        ><strong>{{
          summary.data.value ? formatGB(summary.data.value.file_bytes_exact) : '—'
        }}</strong
        ><small>含归档与删除任务的历史交付</small>
      </div>
      <div class="card">
        <small>预计费用合计</small
        ><strong>{{ summary.data.value ? '¥ ' + summary.data.value.amount : '—' }}</strong
        ><small>单价待设置 {{ summary.data.value?.unset ?? '—' }} 单</small>
      </div>
    </div>
    <p v-if="summary.error.value" role="alert" class="form-error">
      汇总读取失败：{{ summary.error.value }}
    </p>
    <section class="card task-list-card">
      <div class="section-heading">
        <div class="inline-heading">
          <h2>客户任务</h2>
          <small class="muted">本页 {{ list.data.value?.items.length ?? '—' }} 项</small>
        </div>
        <div class="toolbar">
          <input
            id="search"
            v-model="query"
            aria-label="搜索客户任务"
            placeholder="搜索客户、名称或编号"
            maxlength="200"
          /><select id="state-filter" v-model="lifecycle" aria-label="任务范围">
            <option value="active">未归档任务</option>
            <option value="archived">已归档任务</option></select
          ><button @click="refresh()">刷新</button>
        </div>
      </div>
      <p v-if="list.error.value" id="page-error" role="alert" class="form-error">
        {{ list.error.value }}
      </p>
      <p v-if="list.loading.value && !list.data.value" role="status">正在读取任务…</p>
      <div class="table-scroll">
        <table id="task-table">
          <thead>
            <tr>
              <th>任务 / 客户</th>
              <th>状态</th>
              <th>处理进度</th>
              <th>处理结果</th>
              <th>历史交付</th>
              <th>预计费用</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="job in list.data.value?.items" :key="job.id">
              <td>
                <RouterLink class="text-link" :to="{ name: 'job', params: { id: job.id } }">{{
                  job.name
                }}</RouterLink
                ><small class="cell-note">{{ job.customer }} · {{ job.id.slice(0, 8) }}</small>
                <div class="delivery-tags">
                  <span
                    v-for="kind in job.delivery?.contract?.spec.outputs"
                    :key="kind"
                    class="badge"
                    >{{ outputLabels[kind] }}</span
                  >
                </div>
              </td>
              <td>
                <span class="badge">{{ jobStates[job.state] || job.state }}</span>
              </td>
              <td>
                <progress
                  max="100"
                  :value="job.progress_percent || 0"
                  :aria-label="job.name + '处理完成率'"
                ></progress
                ><small class="cell-note">{{ job.completed }} / {{ job.total }} 已处理</small>
              </td>
              <td>
                {{ job.succeeded }} 处理完成<RouterLink
                  class="cell-note text-link"
                  :to="{
                    name: 'job',
                    params: { id: job.id },
                    query: { tab: 'videos', status: 'failed' },
                  }"
                  >{{ job.failed }} 失败</RouterLink
                >
              </td>
              <td>
                {{ formatGB(job.file_bytes_exact)
                }}<small class="cell-note">{{ job.file_count }} 个文件</small>
              </td>
              <td>
                {{ amount(job)
                }}<small class="cell-note">{{
                  job.price_set ? job.price + ' 元 / ' + job.unit : '请设置任务单价'
                }}</small>
              </td>
              <td>
                <div class="actions">
                  <RouterLink class="text-link" :to="{ name: 'job', params: { id: job.id } }"
                    >查看任务</RouterLink
                  ><RouterLink :to="{ path: '/workbench', query: { job: job.id } }"
                    >前往下载 ↗</RouterLink
                  ><button
                    class="danger"
                    data-action="delete"
                    :data-job-id="job.id"
                    :disabled="!editable"
                    @click="deleting = job"
                  >
                    删除
                  </button>
                </div>
              </td>
            </tr>
            <tr v-if="list.data.value && !list.data.value.items.length">
              <td colspan="7" class="empty">暂无符合条件的任务。</td>
            </tr>
          </tbody>
        </table>
      </div>
      <div class="pager">
        <button
          id="jobs-prev"
          :disabled="!history.length || list.loading.value"
          @click="page(false)"
        >
          上一页</button
        ><span>第 {{ history.length + 1 }} 页</span
        ><button
          id="jobs-next"
          :disabled="!list.data.value?.next_cursor || list.loading.value"
          @click="page(true)"
        >
          下一页
        </button>
      </div>
    </section>
    <WorkspaceDialog
      v-if="creating"
      id="editor"
      :open="true"
      title="新建任务"
      :busy="formBusy"
      :before-close="confirmNavigation"
      @close="creating = false"
      ><p>先建立任务，再进入任务添加视频。创建后不会自动下载。</p>
      <JobForm @saved="created" @busy="formBusy = $event"
    /></WorkspaceDialog>
    <TaskConfirmation
      v-if="deleting"
      :job="deleting"
      action="delete"
      @close="deleting = undefined"
      @completed="deleted"
    />
  </main>
</template>
