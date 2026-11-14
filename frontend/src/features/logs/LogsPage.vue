<script setup lang="ts">
import { computed, ref } from 'vue';
import { api } from '../../api';
import { usePolling } from '../../composables/polling';
import { useResource } from '../../composables/resource';
import TaskLog from '../../components/TaskLog.vue';
import { videoStates, type Page } from '../jobs/contracts';
import { stageLabel, type RuntimeTask } from '../workbench/contracts';
const query = ref(''),
  status = ref(''),
  view = ref('history');
const cursor = ref<string | null>(null),
  history = ref<Array<string | null>>([]);
const resource = useResource<Page<RuntimeTask>>(),
  selected = ref<RuntimeTask>();
const actions: Record<string, string> = {
  download: '视频下载',
  check: '环境检查',
  import: '清单导入',
  index: '同步素材',
  clear_media: '清空素材',
};
async function refresh(signal?: AbortSignal) {
  const params = new URLSearchParams({
    scope: 'download',
    view: view.value,
    status: status.value,
    q: query.value,
    limit: '50',
  });
  if (cursor.value) params.set('cursor', cursor.value);
  await resource.load((signal) => api.request('/api/tasks?' + params, { signal }), signal);
}
function filter() {
  cursor.value = null;
  history.value = [];
  void refresh();
}
function page(next: boolean) {
  if (next && resource.data.value?.next_cursor) {
    history.value.push(cursor.value);
    cursor.value = resource.data.value.next_cursor;
  } else if (!next) cursor.value = history.value.pop() || null;
  void refresh();
}
usePolling(
  computed(() => !cursor.value && !selected.value),
  refresh,
  10000,
);
</script>
<template>
  <main class="content">
    <div class="page-heading">
      <div>
        <span class="eyebrow">EXECUTION LOGS</span>
        <h1>执行日志</h1>
        <p>汇总执行记录、进度和错误；日志从任务实际执行的实例读取。</p>
      </div>
    </div>
    <section class="card">
      <form class="toolbar" @submit.prevent="filter">
        <input
          v-model="query"
          aria-label="搜索执行记录"
          placeholder="视频 ID 或标题"
          maxlength="200"
        /><select v-model="view" aria-label="记录范围" @change="filter">
          <option value="history">已结束的执行记录</option>
          <option value="active">正在执行的视频</option>
          <option value="">全部记录</option></select
        ><select
          v-model="status"
          aria-label="执行状态"
          :disabled="view === 'active'"
          @change="filter"
        >
          <option value="">全部状态</option>
          <option v-for="(label, key) in videoStates" :key="key" :value="key">
            {{ label }}
          </option></select
        ><button :disabled="resource.loading.value">查询</button>
      </form>
      <p v-if="resource.error.value" class="form-error" role="alert">
        {{ resource.error.value }} <button @click="refresh()">重试</button>
      </p>
      <p v-if="resource.loading.value && !resource.data.value" role="status">正在读取执行记录…</p>
      <div class="table-scroll">
        <table id="execution-records">
          <thead>
            <tr>
              <th>视频 / 操作</th>
              <th>类型</th>
              <th>状态 / 阶段</th>
              <th>实例 / 创建时间</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="task in resource.data.value?.items" :key="task.id">
              <td>
                {{ task.title || task.video_id || task.id
                }}<small v-if="task.error" class="cell-note">{{ task.error.slice(0, 250) }}</small>
              </td>
              <td>{{ actions[task.action] || '其他操作' }}</td>
              <td>
                {{ videoStates[task.status] || task.status
                }}<small v-if="task.progress?.stage" class="cell-note">{{
                  stageLabel(task.progress.stage)
                }}</small>
              </td>
              <td>
                {{ task.instance_id || '当前实例 / 尚未分配'
                }}<small class="cell-note">
                  {{ task.created_at ? new Date(task.created_at).toLocaleString('zh-CN') : '—' }}
                </small>
              </td>
              <td>
                <button data-task-log @click="selected = task">查看详情与日志</button>
              </td>
            </tr>
            <tr v-if="resource.data.value && !resource.data.value.items.length">
              <td colspan="5" class="empty">暂无符合条件的执行记录。</td>
            </tr>
          </tbody>
        </table>
      </div>
      <div class="pager">
        <button
          id="logs-prev"
          :disabled="!history.length || resource.loading.value"
          @click="page(false)"
        >
          上一页</button
        ><button
          id="logs-next"
          :disabled="!resource.data.value?.next_cursor || resource.loading.value"
          @click="page(true)"
        >
          下一页
        </button>
      </div>
    </section>
    <TaskLog
      v-if="selected"
      :key="selected.id"
      :task="selected"
      @close="
        selected = undefined;
        refresh();
      "
    />
  </main>
</template>
