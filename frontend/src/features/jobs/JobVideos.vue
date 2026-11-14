<script setup lang="ts">
import { computed, ref, watch } from 'vue';
import { api } from '../../api';
import { useResource } from '../../composables/resource';
import { useLeaveGuard } from '../../composables/leaveGuard';
import { bootstrap } from '../../bootstrap';
import { ElMessageBox } from 'element-plus';
import WorkspaceDialog from '../../components/WorkspaceDialog.vue';
import TaskLog from '../../components/TaskLog.vue';
import TaskConfirmation from './TaskConfirmation.vue';
import { deliveryStates, outputLabels } from '../delivery/model';
import { jobEndpoint, videoStates, type Job, type Page, type VideoTask } from './contracts';
const props = defineProps<{ job: Job; initialStatus?: string }>();
const emit = defineEmits<{ changed: []; message: [value: string] }>();
const editable = computed(() => bootstrap().role !== 'viewer' && !props.job.archived);
const query = ref(''),
  status = ref(props.initialStatus || ''),
  deliveryStatus = ref(''),
  cursor = ref<string | null>(null),
  history = ref<Array<string | null>>([]),
  selected = ref<string[]>([]);
const list = useResource<Page<VideoTask>>(),
  busy = ref(false),
  actionError = ref(''),
  redelivering = ref(false),
  clearing = ref(false),
  log = ref<VideoTask>();
const allSelected = computed(
  () =>
    !!list.data.value?.items.length &&
    list.data.value.items.every((item) => selected.value.includes(item.id)),
);
useLeaveGuard(() => ({ dirty: false, busy: busy.value }));
async function refresh() {
  const params = new URLSearchParams({ q: query.value });
  if (status.value) params.set('status', status.value);
  if (deliveryStatus.value) params.set('delivery_status', deliveryStatus.value);
  if (cursor.value) params.set('cursor', cursor.value);
  await list.load((signal) =>
    api.request(jobEndpoint(props.job.id) + '/videos?' + params, { signal }),
  );
}
function filter() {
  cursor.value = null;
  history.value = [];
  selected.value = [];
  void refresh();
}
watch([() => props.job.id, () => props.job.total], filter, { immediate: true });
async function mutate(path: string, body: unknown, message: string) {
  if (busy.value || !editable.value) return;
  busy.value = true;
  actionError.value = '';
  try {
    await api.request(jobEndpoint(props.job.id) + path, { method: 'POST', body });
    selected.value = [];
    redelivering.value = false;
    await refresh();
    emit('changed');
    emit('message', message);
  } catch (failure) {
    actionError.value = String(failure);
  } finally {
    busy.value = false;
  }
}
function page(next: boolean) {
  if (next && list.data.value?.next_cursor) {
    history.value.push(cursor.value);
    cursor.value = list.data.value.next_cursor;
  } else if (!next) cursor.value = history.value.pop() || null;
  selected.value = [];
  void refresh();
}
async function waive(task: VideoTask, key: string) {
  try {
    const result = await ElMessageBox.prompt('请填写已与客户确认的缺失原因。', '接受来源缺失', {
      inputValidator: (value) => !!value?.trim() || '必须填写原因',
      confirmButtonText: '确认接受缺失',
      cancelButtonText: '取消',
    });
    await mutate(
      '/requirements/' + task.id + '/' + encodeURIComponent(key) + '/waive',
      { reason: result.value },
      '已记录缺失确认。',
    );
  } catch {
    /* User cancelled. */
  }
}
</script>
<template>
  <section class="card">
    <div class="section-heading">
      <div>
        <h2>视频清单 · {{ job.total }} 个有效视频</h2>
        <p>同任务内去重；不同任务独立处理。</p>
      </div>
      <div class="actions">
        <button
          v-if="job.delivery.contract"
          data-action="redeliver-selected"
          :disabled="!editable || busy || !selected.length"
          @click="redelivering = true"
        >
          补交选中项</button
        ><button
          data-action="retry"
          :disabled="!editable || busy || !job.failed"
          @click="
            mutate(
              '/control',
              { action: 'retry' },
              '失败视频已排队；暂停中的任务需前往工作台开始。',
            )
          "
        >
          重试失败</button
        ><button
          data-action="remove-selected"
          :disabled="!editable || busy || !selected.length"
          @click="mutate('/videos/remove', { ids: selected }, '已移除选中视频；历史交付保留。')"
        >
          移除选中</button
        ><button
          class="danger"
          data-action="clear"
          :disabled="!editable || busy"
          @click="clearing = true"
        >
          清空清单
        </button>
      </div>
    </div>
    <form class="toolbar" @submit.prevent="filter">
      <input
        id="video-query"
        v-model="query"
        aria-label="搜索视频"
        placeholder="视频 ID 或标题"
        maxlength="200"
      /><select id="video-filter" v-model="status" aria-label="视频状态">
        <option value="">全部有效视频</option>
        <option v-for="(label, key) in videoStates" :key="key" :value="key">
          {{ label }}
        </option></select
      ><select id="delivery-status-filter" v-model="deliveryStatus" aria-label="交付状态">
        <option value="">全部交付状态</option>
        <option
          v-for="key in ['pending', 'partial', 'review', 'complete', 'accepted']"
          :key="key"
          :value="key"
        >
          {{ deliveryStates[key] }}
        </option></select
      ><button type="submit">查询 / 刷新</button>
    </form>
    <div class="toolbar">
      <a class="text-link" :href="jobEndpoint(job.id) + '/export'">导出全部 ID</a
      ><a class="text-link" :href="jobEndpoint(job.id) + '/export?kind=failed'">导出失败 ID</a
      ><span class="selection-count">已选择 {{ selected.length }} 条</span>
    </div>
    <p v-if="list.error.value || actionError" role="alert" class="form-error">
      {{ actionError || list.error.value }}
    </p>
    <p v-if="list.loading.value && !list.data.value" role="status">正在读取视频…</p>
    <div class="table-scroll">
      <table>
        <thead>
          <tr>
            <th>
              <input
                type="checkbox"
                :checked="allSelected"
                aria-label="选择本页全部视频"
                :disabled="!editable || busy"
                @change="
                  selected = ($event.target as HTMLInputElement).checked
                    ? list.data.value?.items.map((item) => item.id) || []
                    : []
                "
              />
            </th>
            <th>视频 ID / 标题</th>
            <th>状态</th>
            <th>交付 / 执行阶段</th>
            <th>错误</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="video in list.data.value?.items" :key="video.id">
            <td>
              <input
                v-model="selected"
                type="checkbox"
                :value="video.id"
                :aria-label="'选择 ' + video.video_id"
                :disabled="!editable || busy"
              />
            </td>
            <td>
              <a
                class="text-link"
                :href="'https://www.youtube.com/watch?v=' + encodeURIComponent(video.video_id)"
                target="_blank"
                rel="noopener"
                >{{ video.video_id }}</a
              ><small class="cell-note">{{ video.title }}</small>
            </td>
            <td>
              <span class="badge">{{
                video.delivery?.version && video.status === 'succeeded'
                  ? deliveryStates[video.delivery.status]
                  : video.result?.test_mode
                    ? '测试完成'
                    : video.result?.skipped
                      ? '已跳过'
                      : videoStates[video.status] || video.status
              }}</span>
            </td>
            <td>
              <template v-if="video.delivery?.version"
                ><strong
                  >{{ video.delivery.test_mode ? '测试处理 · ' : ''
                  }}{{ deliveryStates[video.delivery.status] }}</strong
                ><small class="cell-note">要求版本 {{ video.delivery.version }}</small>
                <div
                  v-for="requirement in video.delivery.requirements"
                  :key="requirement.key"
                  class="cell-note"
                >
                  {{ outputLabels[requirement.kind] }} {{ requirement.language }}：{{
                    deliveryStates[requirement.status] || requirement.status
                  }}<span v-if="requirement.error"> · {{ requirement.error }}</span
                  ><button
                    v-if="requirement.status === 'missing'"
                    :disabled="!editable || busy"
                    @click="waive(video, requirement.key)"
                  >
                    接受缺失
                  </button>
                </div></template
              ><small class="cell-note"
                >{{ video.progress?.stage || '—' }} · {{ video.instance_id || '本机' }}</small
              >
            </td>
            <td :title="video.error">{{ video.error?.slice(0, 100) }}</td>
            <td>
              <div class="actions">
                <button
                  :disabled="!editable || busy || video.status === 'removed'"
                  @click="mutate('/videos/remove', { ids: [video.id] }, '视频已移出清单。')"
                >
                  移出清单</button
                ><button @click="log = video">日志</button
                ><button
                  v-if="job.delivery.contract"
                  :disabled="!editable || busy || video.status === 'running'"
                  @click="
                    mutate(
                      '/redeliver',
                      { ids: [video.id], use_latest: false },
                      '已安排按原要求补交缺项。',
                    )
                  "
                >
                  补交缺项
                </button>
              </div>
            </td>
          </tr>
          <tr v-if="list.data.value && !list.data.value.items.length">
            <td colspan="6" class="empty">
              {{
                job.total ? '没有符合筛选条件的视频。' : '该任务还没有视频。点击上方“添加视频”。'
              }}
            </td>
          </tr>
        </tbody>
      </table>
    </div>
    <div class="pager">
      <button :disabled="!history.length || list.loading.value || busy" @click="page(false)">
        上一页</button
      ><span>第 {{ history.length + 1 }} 页 · 每页 50 条</span
      ><button
        :disabled="!list.data.value?.next_cursor || list.loading.value || busy"
        @click="page(true)"
      >
        下一页
      </button>
    </div>
    <WorkspaceDialog
      :open="redelivering"
      title="补交选中来源"
      :busy="busy"
      @close="redelivering = false"
      ><p>
        已选择
        {{ selected.length }}
        条来源，请明确选择使用的交付要求。已验证可读取且语义相同的正式文件可复用。
      </p>
      <p v-if="actionError" role="alert" class="form-error">{{ actionError }}</p>
      <div class="actions">
        <button
          :disabled="busy"
          @click="
            mutate('/redeliver', { ids: selected, use_latest: false }, '已按原要求安排补交。')
          "
        >
          沿用原要求</button
        ><button
          :disabled="busy"
          @click="
            mutate('/redeliver', { ids: selected, use_latest: true }, '已按最新要求安排补交。')
          "
        >
          使用最新要求</button
        ><button :disabled="busy" @click="redelivering = false">取消</button>
      </div></WorkspaceDialog
    >
    <TaskConfirmation
      v-if="clearing"
      :job="job"
      action="clear"
      @close="clearing = false"
      @completed="
        clearing = false;
        filter();
        emit('changed');
      "
    />
    <TaskLog v-if="log" :key="log.id" :task="log" @close="log = undefined" />
  </section>
</template>
