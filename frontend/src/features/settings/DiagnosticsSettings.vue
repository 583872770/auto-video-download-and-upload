<script setup lang="ts">
import { computed, ref } from 'vue';
import { api } from '../../api';
import { useInstances } from '../../stores/instances';
import { usePolling } from '../../composables/polling';
import type { InstanceStatus } from '../../api/types';
const instances = useInstances();
const status = ref<InstanceStatus>(),
  error = ref(''),
  updated = ref('');
async function refresh(signal: AbortSignal) {
  const scope = instances.scope();
  try {
    status.value = await api.request('/api/status', { signal, scope, cache: 'no-store' });
    updated.value = new Date().toLocaleTimeString();
    error.value = '';
  } catch (failure) {
    if (scope.isCurrent() && !signal.aborted) error.value = String(failure);
    throw failure;
  }
}
const { refresh: reload } = usePolling(
  computed(() => true),
  refresh,
  15000,
);
const risks = computed(() => {
  const value = status.value?.concurrency.risks;
  if (Array.isArray(value)) return value.map((item) => item.detail || item.code);
  return [];
});
</script>
<template>
  <section class="card">
    <div class="card-heading">
      <h2>运行诊断</h2>
      <button type="button" @click="reload">刷新</button>
    </div>
    <p>只读状态，页面可见时每 15 秒刷新。{{ updated ? '更新于 ' + updated : '正在读取…' }}</p>
    <p v-if="error" role="alert" class="error">{{ error }}</p>
    <template v-if="status"
      ><div class="metrics">
        <div>
          <small>数据库</small><strong>{{ status.database }}</strong>
        </div>
        <div>
          <small>当前实例</small><strong>{{ instances.name }}</strong>
        </div>
      </div>
      <h3>执行容量</h3>
      <div class="metrics">
        <div>
          <small>运行 / 有效额度</small
          ><strong
            >{{ status.concurrency.running }} / {{ status.concurrency.effective_limit }}</strong
          >
        </div>
        <div>
          <small>使用连接 / 连接预算</small
          ><strong
            >{{ status.concurrency.connection_used }} /
            {{ status.concurrency.connection_budget }}</strong
          >
        </div>
      </div>
      <p>
        传输 {{ status.concurrency.transferring }} · 准备 {{ status.concurrency.preparing }} ·
        后处理 {{ status.concurrency.processing }}
      </p>
      <p>{{ status.concurrency.bottleneck }}</p>
      <h3>运行工具</h3>
      <div class="tool-grid">
        <div v-for="(value, name) in status.tools" :key="name">
          <span>{{ name }}</span
          ><span class="badge" :class="{ warning: !value }">{{ value ? '可用' : '未配置' }}</span>
        </div>
      </div>
      <h3>风险提示</h3>
      <ul v-if="risks.length">
        <li v-for="risk in risks" :key="risk">{{ risk }}</li>
      </ul>
      <p v-else>暂无风险条目。可展开容量详情进一步排查。</p>
      <details>
        <summary>并发与资源详情</summary>
        <pre class="report">{{ JSON.stringify(status.concurrency, null, 2) }}</pre>
      </details>
    </template>
  </section>
</template>
