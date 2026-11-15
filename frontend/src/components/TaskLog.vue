<script setup lang="ts">
import { computed, watch } from 'vue';
import { api } from '../api';
import { useResource } from '../composables/resource';
import { usePolling } from '../composables/polling';
import { stageLabel } from '../features/workbench/contracts';
import { videoStates, formatGB } from '../features/jobs/contracts';
import { logTime, readableLog } from '../features/logs/presentation';
import { appendLog, readTaskData, type LogResponse, type LogState } from '../features/logs/reading';
import WorkspaceDialog from './WorkspaceDialog.vue';
interface Detail {
  id: string;
  title: string;
  video_id: string;
  status: string;
  action: string;
  created_at: string;
  started_at?: string;
  finished_at?: string;
  error?: string;
  progress?: {
    stage?: string;
    percent?: number;
    speed?: number;
    deleted_files?: number;
    deleted_bytes?: number;
    failed_files?: number;
    scanned?: number;
    added?: number;
    duplicates?: number;
    invalid?: number;
  };
  result?: { message?: string };
  risk?: {
    retry_scheduled: boolean;
    attempts: number;
    max_attempts: number;
    available_at: number;
    failure?: {
      kind: string;
      message?: string;
      http_status?: number;
      observed_at?: number;
      evidence?: string;
    };
  };
}
const props = defineProps<{ task: { id: string; video_id?: string; instance_id?: string } }>();
const emit = defineEmits<{ close: [] }>();
const identity = computed(() => props.task.id);
const status = useResource<Detail>();
const logs = useResource<LogState>();
const detail = computed(() => status.data.value);
const entries = computed(() => readableLog(logs.data.value?.text || ''));
const refreshing = computed(() => status.loading.value || logs.loading.value);
function refresh(signal?: AbortSignal) {
  const id = identity.value,
    previous = logs.data.value;
  const scope = props.task.instance_id
    ? { id: props.task.instance_id, epoch: 0, isCurrent: () => identity.value === id }
    : undefined;
  return Promise.all([
    status.load(
      (signal) =>
        readTaskData(
          () => api.request<Detail>('/api/tasks/' + encodeURIComponent(id), { signal, scope }),
          signal,
        ),
      signal,
    ),
    logs.load(async (signal) => {
      const response = await readTaskData(
        () =>
          api.request<LogResponse>(
            '/api/tasks/' + encodeURIComponent(id) + '/logs?offset=' + (previous?.offset || 0),
            { signal, scope },
          ),
        signal,
      );
      return appendLog(previous, response);
    }, signal),
  ]).then(() => undefined);
}
watch(identity, () => {
  status.clear();
  logs.clear();
  void refresh();
});
usePolling(
  computed(() => true),
  refresh,
  5000,
);
</script>
<template>
  <WorkspaceDialog :open="true" title="执行日志" @close="emit('close')">
    <div class="actions">
      <button :disabled="refreshing" @click="refresh()">
        {{ refreshing ? '正在读取…' : '刷新日志' }}
      </button>
    </div>
    <p v-if="logs.error.value" class="form-error" role="alert">
      日志读取：{{ logs.error.value }} 已读取的内容会保留，可点击刷新重试。
    </p>
    <p v-if="status.error.value" class="form-error" role="alert">
      任务状态读取：{{ status.error.value }} 日志会独立更新。
    </p>
    <template v-if="detail"
      ><h3>{{ detail.title || detail.video_id || detail.id }}</h3>
      <p>
        {{ videoStates[detail.status] || detail.status }} · {{ stageLabel(detail.progress?.stage)
        }}<span v-if="detail.progress?.percent != null">
          · {{ detail.progress.percent.toFixed(1) }}%</span
        >
      </p>
      <p class="cell-note">
        创建 {{ logTime(detail.created_at)
        }}<template v-if="detail.started_at"> · 开始 {{ logTime(detail.started_at) }}</template
        ><template v-if="detail.finished_at"> · 结束 {{ logTime(detail.finished_at) }}</template>
      </p>
      <p v-if="detail.error" class="form-error">{{ detail.error }}</p>
      <p v-if="detail.result?.message" class="notice">{{ detail.result.message }}</p>
      <p v-if="detail.risk?.failure">
        故障记录：{{ detail.risk.failure.message || detail.risk.failure.kind
        }}<template v-if="detail.risk.failure.http_status">
          · HTTP {{ detail.risk.failure.http_status }}</template
        >。历史故障不代表当前仍受限。
      </p>
      <p v-if="detail.risk?.retry_scheduled">
        已安排重试 {{ detail.risk.attempts }} / {{ detail.risk.max_attempts }} 次 · 下次检查
        {{
          new Date(detail.risk.available_at * 1000).toLocaleString('zh-CN')
        }}；到期后仍需满足资源条件。
      </p>
      <p v-if="detail.action === 'clear_media'">
        已删除 {{ detail.progress?.deleted_files || 0 }} 个文件 · 释放
        {{ formatGB(detail.progress?.deleted_bytes) }} · 删除失败
        {{ detail.progress?.failed_files || 0 }} 个
      </p>
      <p v-if="detail.progress?.scanned">已扫描 {{ detail.progress.scanned }} 个文件</p>
      <p v-if="detail.action === 'import'">
        已添加 {{ detail.progress?.added || 0 }} 条 · 重复 {{ detail.progress?.duplicates || 0 }} 条
        · 无效 {{ detail.progress?.invalid || 0 }} 条
      </p>
    </template>
    <ol v-if="entries.length" id="log-content" class="task-log-lines">
      <li v-for="(entry, index) in entries" :key="index" :class="entry.level">
        <time v-if="entry.time" :datetime="entry.time">{{ logTime(entry.time) }}</time>
        <span>{{ entry.text }}</span>
      </li>
    </ol>
    <p v-else class="cell-note">
      {{
        logs.data.value?.message ||
        (logs.loading.value
          ? '正在读取日志…'
          : logs.data.value?.text
            ? '暂无可展示的过程说明，可展开原始日志查看。'
            : '尚未产生日志。')
      }}
    </p>
    <details v-if="logs.data.value?.text" class="task-log-raw">
      <summary>原始日志（技术排查）</summary>
      <pre id="raw-log-content" class="log-output">{{ logs.data.value.text }}</pre>
    </details>
  </WorkspaceDialog>
</template>
<style scoped>
.task-log-lines {
  list-style: none;
  margin: 16px 0;
  padding: 0;
  max-height: 50vh;
  overflow: auto;
}
.task-log-lines li {
  display: grid;
  grid-template-columns: 145px 1fr;
  gap: 12px;
  padding: 10px 12px;
  border-bottom: 1px solid var(--line);
  font-size: 13px;
  line-height: 1.65;
  overflow-wrap: anywhere;
}
.task-log-lines time {
  color: #64748b;
  font-size: 12px;
  font-variant-numeric: tabular-nums;
}
.task-log-lines .success span {
  color: #0f766e;
}
.task-log-lines .warning {
  background: #fffbeb;
}
.task-log-lines .warning span {
  color: #92400e;
}
.task-log-lines .error span {
  color: #b42318;
}
.task-log-raw summary {
  cursor: pointer;
  color: #64748b;
  font-size: 13px;
}
@media (max-width: 600px) {
  .task-log-lines li {
    grid-template-columns: 1fr;
    gap: 2px;
  }
}
</style>
