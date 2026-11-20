<script setup lang="ts">
import { computed, ref } from 'vue';
import { RouterLink } from 'vue-router';
import { ElButton, ElMessageBox } from 'element-plus';
import { api } from '../../api';
import { bootstrap } from '../../bootstrap';
import { useInstances } from '../../stores/instances';
import { usePolling } from '../../composables/polling';
import { diskBytes } from './model';
import { logTime } from '../logs/presentation';
import {
  actions,
  confirmation,
  type FailedTest,
  type FailedTestsPage,
  type Resolution,
} from './failedTests';

const emit = defineEmits<{ resolved: [] }>();
const instances = useInstances();
const items = ref<FailedTest[]>([]),
  cursor = ref<string | null>(null);
const error = ref(''),
  feedback = ref(''),
  loading = ref(false),
  busy = ref(false);
const admin = bootstrap().role === 'admin';
const active = computed(() => !busy.value);
let generation = 0;
async function load(signal?: AbortSignal, append = false) {
  const scope = instances.scope(),
    request = ++generation;
  loading.value = true;
  try {
    const query = append && cursor.value ? '?cursor=' + encodeURIComponent(cursor.value) : '';
    const result = await api.request<FailedTestsPage>('/api/disk/failed-tests' + query, {
      scope,
      signal,
    });
    if (!scope.isCurrent() || request !== generation) return;
    items.value = append ? [...items.value, ...result.items] : result.items;
    cursor.value = result.next_cursor;
    error.value = '';
  } catch (failure) {
    if (scope.isCurrent() && !signal?.aborted) error.value = String(failure);
  } finally {
    if (scope.isCurrent() && request === generation) loading.value = false;
  }
}
usePolling(active, (signal) => load(signal), 30000);
async function resolve(item: FailedTest, action: Resolution) {
  const scope = instances.scope();
  busy.value = true;
  try {
    await ElMessageBox.confirm(confirmation(item, action, instances.name), actions[action], {
      confirmButtonText: actions[action],
      cancelButtonText: '取消',
      type: 'warning',
    });
    if (!scope.isCurrent()) return;
    const result = await api.request<{ message: string }>('/api/disk/failed-tests/resolve', {
      method: 'POST',
      scope,
      body: { task_id: item.task_id, version: item.version, action, confirmed: true },
    });
    feedback.value = result.message;
    await load();
    emit('resolved');
  } catch (failure) {
    if (scope.isCurrent() && failure !== 'cancel' && failure !== 'close')
      error.value = String(failure);
  } finally {
    busy.value = false;
  }
}
</script>
<template>
  <section class="failed-tests" aria-label="失败测试处理">
    <h3>失败测试处理</h3>
    <p class="hint">
      这些测试已停止，不会自动继续。选择重新下载、重试处理，或放弃剩余处理并释放文件；失败记录和已登记统计都会保留。
    </p>
    <p v-if="error" class="error" role="alert">{{ error }}</p>
    <p v-if="feedback" role="status">{{ feedback }}</p>
    <ElButton :disabled="busy || loading" @click="load()">刷新失败测试</ElButton>
    <p v-if="!items.length && !loading && !error">没有待处理的失败测试文件。</p>
    <p v-if="loading && !items.length">正在读取失败测试…</p>
    <article v-for="item in items" :key="item.task_id" class="failed-test">
      <strong>{{ item.video_id }} · {{ item.title || item.job_name || '测试任务' }}</strong>
      <p>{{ item.error }}</p>
      <p>
        保留文件 {{ diskBytes(item.bytes) }} · 已处理 {{ item.attempts }} 次 · 失败于
        {{ logTime(item.failed_at) }}
      </p>
      <p v-if="item.recommendation === 'redownload'">
        源文件损坏或缺少轨道，请重新下载；重复转换同一文件无法修复。
      </p>
      <p v-else>若源文件损坏请重新下载；若已修复处理环境，可重试本地处理。</p>
      <div class="actions">
        <ElButton :disabled="!admin || busy || loading" @click="resolve(item, 'redownload')"
          >重新下载</ElButton
        >
        <ElButton
          :disabled="!admin || busy || loading || item.recommendation === 'redownload'"
          @click="resolve(item, 'retry')"
          >重试本地处理</ElButton
        >
        <ElButton :disabled="!admin || busy || loading" @click="resolve(item, 'discard')"
          >放弃并清理</ElButton
        >
        <RouterLink v-if="item.job_id" :to="'/jobs/' + item.job_id">查看任务</RouterLink>
      </div>
    </article>
    <ElButton v-if="cursor" :disabled="busy || loading" @click="load(undefined, true)"
      >加载更多失败测试</ElButton
    >
    <p v-if="!admin" class="hint">请由管理员执行文件恢复或清理。</p>
  </section>
</template>
<style scoped>
.failed-tests {
  margin: 1rem 0;
}
.failed-test {
  margin-top: 0.8rem;
  padding: 1rem;
  border: 1px solid #d9e2ee;
  border-radius: 8px;
  overflow-wrap: anywhere;
}
.failed-test p {
  margin: 0.5rem 0;
}
.actions {
  display: flex;
  flex-wrap: wrap;
  gap: 0.5rem;
  align-items: center;
}
.actions :deep(.el-button + .el-button) {
  margin-left: 0;
}
</style>
