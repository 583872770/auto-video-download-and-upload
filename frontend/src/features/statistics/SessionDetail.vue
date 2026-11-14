<script setup lang="ts">
import { computed, ref, watch } from 'vue';
import { api } from '../../api';
import { bootstrap } from '../../bootstrap';
import WorkspaceDialog from '../../components/WorkspaceDialog.vue';
import TaskLog from '../../components/TaskLog.vue';
import { useResource } from '../../composables/resource';
import { usePolling } from '../../composables/polling';
import { useOperation } from '../../composables/operation';
import { confirmNavigation, useLeaveGuard } from '../../composables/leaveGuard';
import MethodTable from './MethodTable.vue';
import { gb, time, type Session, type Entry, type Method } from './contracts';
const props = defineProps<{ id: string }>(),
  emit = defineEmits<{ close: []; saved: [] }>();
const resource = useResource<{
    session: Session;
    items: Entry[];
    next_cursor: string | null;
    methods: Method[];
  }>(),
  op = useOperation();
const label = ref(''),
  baseline = ref(''),
  kind = ref('files'),
  cursor = ref(''),
  history = ref<string[]>([]),
  log = ref<string>();
const dirty = computed(() => label.value !== baseline.value);
const canEdit = bootstrap().role !== 'viewer';
useLeaveGuard(() => ({ dirty: dirty.value, busy: op.busy.value }));
async function refresh(signal?: AbortSignal) {
  await resource.load(
    (signal) =>
      api.request(
        `/api/statistics/sessions/${props.id}?` +
          new URLSearchParams({ kind: kind.value, cursor: cursor.value }),
        { signal },
      ),
    signal,
  );
  if (!dirty.value && resource.data.value)
    label.value = baseline.value = resource.data.value.session.label || '';
}
function page(next: boolean) {
  if (next) {
    history.value.push(cursor.value);
    cursor.value = resource.data.value?.next_cursor || '';
  } else cursor.value = history.value.pop() || '';
  void refresh();
}
watch(kind, () => {
  cursor.value = '';
  history.value = [];
  void refresh();
});
async function save() {
  await op.run(async () => {
    await api.request(`/api/statistics/sessions/${props.id}`, {
      method: 'PUT',
      body: { label: label.value },
    });
    const expected = label.value;
    await refresh();
    if (resource.error.value || resource.data.value?.session.label !== expected)
      throw new Error('备注尚未读回确认，输入已保留。');
    baseline.value = expected;
    emit('saved');
  });
}
usePolling(
  computed(() => !op.busy.value),
  refresh,
  5000,
);
</script>
<template>
  <WorkspaceDialog
    :open="true"
    title="执行会话明细"
    :busy="op.busy.value"
    :before-close="confirmNavigation"
    @close="emit('close')"
    ><p v-if="resource.error.value || op.error.value" class="notice error" role="alert">
      {{ resource.error.value || op.error.value }}
    </p>
    <template v-if="resource.data.value"
      ><p>
        {{ time(resource.data.value.session.started_at) }} 至
        {{ time(resource.data.value.session.ended_at) }} · 会话状态
        {{ resource.data.value.session.status }}
      </p>
      <p>
        交付 {{ gb(resource.data.value.session.file_bytes_exact) }} · 测试
        {{ gb(resource.data.value.session.test_file_bytes_exact) }}（不计费）
      </p>
      <p class="notice">执行会话只记录运行结果，费用按客户任务汇总。</p>
      <form class="toolbar" @submit.prevent="save">
        <label>会话备注<input v-model="label" :disabled="!canEdit" maxlength="200" /></label
        ><button :disabled="!canEdit || op.busy.value || !dirty">保存备注</button>
      </form>
      <MethodTable :items="resource.data.value.methods" /><a
        :href="`/api/statistics/sessions/${id}/export?kind=methods`"
        download
        >导出提供方统计</a
      >
      <div class="toolbar">
        <label
          >明细范围<select v-model="kind">
            <option value="files">交付文件</option>
            <option value="test_files">测试文件（已删除）</option>
            <option value="failed">失败任务</option>
            <option value="tasks">全部任务</option>
          </select></label
        ><a :href="`/api/statistics/sessions/${id}/export?kind=${kind}`" download
          >导出本类明细 CSV</a
        >
      </div>
      <div class="table-scroll">
        <table>
          <thead>
            <tr>
              <th>视频 / 文件</th>
              <th>大小 / 结果</th>
              <th>时间 / 原因</th>
              <th>日志</th>
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="row in resource.data.value.items"
              :key="row.task_id + row.path + row.video_id"
            >
              <td>
                {{ row.title || row.video_id || '链接任务'
                }}<small class="cell-note">{{ row.path }}</small>
              </td>
              <td>
                {{
                  ['files', 'test_files'].includes(kind)
                    ? gb(row.size_exact || row.size)
                    : row.status
                }}<small v-if="row.size_exact" class="cell-note">{{ row.size_exact }} 字节</small>
              </td>
              <td>
                {{ time(row.created_at || row.finished_at || row.started_at)
                }}<small class="cell-note">{{ row.error }}</small>
              </td>
              <td><button @click="log = row.task_id">查看日志</button></td>
            </tr>
            <tr v-if="!resource.data.value.items.length">
              <td colspan="4" class="empty">暂无记录。</td>
            </tr>
          </tbody>
        </table>
      </div>
      <div class="pager">
        <button :disabled="!history.length || resource.loading.value" @click="page(false)">
          上一页</button
        ><button
          :disabled="!resource.data.value.next_cursor || resource.loading.value"
          @click="page(true)"
        >
          下一页
        </button>
      </div></template
    ><TaskLog v-if="log" :key="log" :task="{ id: log }" @close="log = undefined"
  /></WorkspaceDialog>
</template>
