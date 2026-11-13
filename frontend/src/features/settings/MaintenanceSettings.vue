<script setup lang="ts">
import { ref, computed } from 'vue';
import { ElButton, ElMessageBox } from 'element-plus';
import 'element-plus/es/components/button/style/css';
import 'element-plus/es/components/message-box/style/css';
import { api } from '../../api';
import { bootstrap } from '../../bootstrap';
import { useInstances } from '../../stores/instances';
import { usePolling } from '../../composables/polling';
import { groups } from './fields';
import FieldGroup from './FieldGroup.vue';
import type { DiskSnapshot, RuntimeSummary } from '../../api/types';
import { summaryText } from './reports';
const instances = useInstances();
const disk = ref<DiskSnapshot>();
const error = ref(''),
  text = ref(''),
  busy = ref(false),
  report = ref('');
const active = computed(() => true);
const admin = bootstrap().role === 'admin';
async function refresh(signal?: AbortSignal) {
  const scope = instances.scope();
  try {
    disk.value = await api.request('/api/disk', { scope, signal, cache: 'no-store' });
    error.value = '';
  } catch (failure) {
    if (scope.isCurrent() && !signal?.aborted) error.value = String(failure);
    throw failure;
  }
}
usePolling(active, refresh);
const gib = (n: number) => (n / 1024 ** 3).toFixed(1) + ' GiB';
async function cleanup() {
  try {
    await ElMessageBox.confirm(
      '将按当前保留策略清理可回收的临时文件与过期日志。是否立即清理？',
      '清理当前实例',
      { confirmButtonText: '立即清理', cancelButtonText: '取消', type: 'warning' },
    );
  } catch {
    return;
  }
  busy.value = true;
  const scope = instances.scope();
  try {
    const result = await api.request<{ message?: string }>('/api/disk/cleanup', {
      method: 'POST',
      body: {},
      scope,
    });
    text.value = result.message || '清理完成。';
    await refresh();
  } catch (failure) {
    error.value = String(failure);
  } finally {
    busy.value = false;
  }
}
async function showReport(kind: 'logs' | 'summary', days = 1) {
  busy.value = true;
  try {
    const scope = instances.scope();
    report.value =
      kind === 'logs'
        ? (await api.request<{ text: string }>('/api/disk/logs?scope=current', { scope })).text ||
          '暂无日志。'
        : summaryText(
            await api.request<RuntimeSummary>(`/api/disk/runtime-summary?days=${days}`, { scope }),
          );
  } catch (failure) {
    error.value = String(failure);
  } finally {
    busy.value = false;
  }
}
</script>
<template>
  <section class="card">
    <h2>当前空间</h2>
    <p v-if="error" class="error" role="alert">{{ error }}</p>
    <div v-if="disk" class="metrics">
      <div>
        <small>磁盘可用</small><strong>{{ gib(disk.free_bytes) }}</strong>
      </div>
      <div>
        <small>受管临时文件</small><strong>{{ gib(disk.used_bytes) }}</strong>
      </div>
      <div>
        <small>临时文件预算</small><strong>{{ gib(disk.budget_bytes) }}</strong>
      </div>
      <div>
        <small>等待空间任务</small><strong>{{ disk.waiting_tasks }}</strong>
      </div>
    </div>
    <p v-if="disk?.message">{{ disk.message }}</p>
    <p v-if="text" role="status">{{ text }}</p>
    <div class="actions">
      <ElButton :disabled="busy" @click="refresh().catch(() => {})">刷新空间</ElButton
      ><ElButton :disabled="!admin || busy" @click="cleanup">清理可回收文件</ElButton
      ><ElButton :disabled="busy" @click="showReport('logs')">近期日志</ElButton
      ><ElButton :disabled="busy" @click="showReport('summary', 1)">近一天汇总</ElButton
      ><ElButton :disabled="busy" @click="showReport('summary', 7)">近七天汇总</ElButton>
    </div>
    <details v-if="report" open>
      <summary>排查内容</summary>
      <pre class="report">{{ report }}</pre>
    </details>
  </section>
  <fieldset :disabled="!admin">
    <FieldGroup title="空间水位与总预算" :fields="groups.disk" />
    <FieldGroup title="分类空间预算" :fields="groups.budgets" />
    <FieldGroup title="日志保留" :fields="groups.logs" />
  </fieldset>
</template>
