<script setup lang="ts">
import { computed, watch } from 'vue';
import { api } from '../api';
import { useResource } from '../composables/resource';
import { usePolling } from '../composables/polling';
import { stageLabel } from '../features/workbench/contracts';
import { videoStates, formatGB } from '../features/jobs/contracts';
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
interface LogResponse {
  text: string;
  offset: number;
  message?: string;
  pruned?: boolean;
}
const props = defineProps<{ task: { id: string; video_id?: string; instance_id?: string } }>();
const emit = defineEmits<{ close: [] }>();
const identity = computed(() => props.task.id);
const resource = useResource<{ detail: Detail; text: string; offset: number; message?: string }>();
const detail = computed(() => resource.data.value?.detail);
function refresh(signal?: AbortSignal) {
  const id = identity.value,
    previous = resource.data.value;
  const scope = props.task.instance_id
    ? { id: props.task.instance_id, epoch: 0, isCurrent: () => identity.value === id }
    : undefined;
  return resource.load(async (signal) => {
    const [detail, logs] = await Promise.all([
      api.request<Detail>('/api/tasks/' + encodeURIComponent(id), { signal, scope }),
      api.request<LogResponse>(
        '/api/tasks/' + encodeURIComponent(id) + '/logs?offset=' + (previous?.offset || 0),
        { signal, scope },
      ),
    ]);
    return {
      detail,
      offset: logs.offset,
      message: logs.message,
      text: ((logs.pruned ? '[早期日志已自动清理]\n' : previous?.text || '') + logs.text).slice(
        -200000,
      ),
    };
  }, signal);
}
watch(identity, () => {
  resource.clear();
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
      <button @click="refresh()">刷新日志</button>
    </div>
    <p v-if="resource.error.value" class="form-error" role="alert">{{ resource.error.value }}</p>
    <template v-if="detail"
      ><h3>{{ detail.title || detail.video_id || detail.id }}</h3>
      <p>
        {{ videoStates[detail.status] || detail.status }} · {{ stageLabel(detail.progress?.stage)
        }}<span v-if="detail.progress?.percent != null">
          · {{ detail.progress.percent.toFixed(1) }}%</span
        >
      </p>
      <p class="cell-note">
        创建 {{ detail.created_at
        }}<template v-if="detail.started_at"> · 开始 {{ detail.started_at }}</template
        ><template v-if="detail.finished_at"> · 结束 {{ detail.finished_at }}</template>
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
    <pre id="log-content" class="log-output">{{
      resource.data.value?.text ||
      resource.data.value?.message ||
      (resource.loading.value ? '正在读取日志…' : '尚未产生日志。')
    }}</pre>
  </WorkspaceDialog>
</template>
