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
import DiskDiagnosis from '../../components/DiskDiagnosis.vue';
import FailedTests from '../disk/FailedTests.vue';
import type { DiskSnapshot, RuntimeSummary } from '../../api/types';
import { summaryText } from './reports';
import { RouterLink } from 'vue-router';
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
    <h2>空间是否影响下载</h2>
    <p v-if="error" class="error" role="alert">{{ error }}</p>
    <p class="hint">先看是否影响下载，再看受限原因和处理方式。磁盘使用率与程序临时额度分别判断。</p>
    <div class="actions">
      <ElButton :disabled="busy" @click="refresh().catch(() => {})">刷新空间</ElButton>
      <ElButton :disabled="!admin || busy" @click="cleanup">清理可回收文件</ElButton>
    </div>
    <DiskDiagnosis v-if="disk?.diagnostic?.download" :diagnostic="disk.diagnostic" expanded />
    <p v-else role="status">空间状态待更新，请刷新或确认实例在线。</p>
    <p v-if="text" role="status">{{ text }}</p>
    <FailedTests @resolved="refresh().catch(() => {})" />
    <h3>测试样本自动回收</h3>
    <p v-if="disk?.test_samples">
      当前保留 {{ disk.test_samples.count ?? '—' }} 份，实际文件
      {{ ((disk.test_samples.used_bytes || 0) / 1048576).toFixed(1) }} MiB；实例全部样本上限
      {{ (disk.test_samples.budget_bytes / 1048576).toFixed(0) }} MiB。包含在测试临时额度内。
    </p>
    <p>
      每 30
      秒检查数量、容量、到期时间和服务器空间。优先回收已结束且未在查看的旧样本；正在下载和处理的文件受保护。
    </p>
    <p v-if="disk?.test_samples?.last_cleanup.reason" role="status">
      最近回收：{{ disk.test_samples.last_cleanup.reason }}
    </p>
    <p v-if="disk?.test_samples?.last_cleanup.error" class="form-error" role="alert">
      {{ disk.test_samples.last_cleanup.error }}
    </p>
    <RouterLink class="text-link" to="/jobs?view=videos">在任务中心核对或清理样本 →</RouterLink>
    <h3>日志与运行排查</h3>
    <p>技术日志用于排查问题。清理过期日志后，任务记录、统计和正式素材继续保留。</p>
    <div class="actions">
      <ElButton :disabled="busy" @click="showReport('logs')">近期日志</ElButton
      ><ElButton :disabled="busy" @click="showReport('summary', 1)">近一天汇总</ElButton
      ><ElButton :disabled="busy" @click="showReport('summary', 7)">近七天汇总</ElButton>
    </div>
    <details v-if="report" open>
      <summary>排查内容</summary>
      <pre class="report">{{ report }}</pre>
    </details>
  </section>
  <fieldset :disabled="!admin">
    <FieldGroup title="磁盘保护与临时文件总额度" :fields="groups.disk" />
    <FieldGroup title="各类临时文件额度" :fields="groups.budgets" />
    <FieldGroup title="日志保留" :fields="groups.logs" />
  </fieldset>
</template>
