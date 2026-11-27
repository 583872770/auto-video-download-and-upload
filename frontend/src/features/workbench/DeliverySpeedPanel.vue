<script setup lang="ts">
import { computed, ref } from 'vue';
import { api } from '../../api';
import { usePolling } from '../../composables/polling';
import { useResource } from '../../composables/resource';
import { useInstances } from '../../stores/instances';
import { formatGB } from '../jobs/contracts';
import type { DeliverySpeed } from './contracts';

const instances = useInstances();
const history = useResource<DeliverySpeed>();
const data = history.data;
const showDetails = ref(false);
const maximum = computed(() =>
  Math.max(1, ...(data.value?.points || []).map((point) => point.mbps)),
);
const hasDeliveries = computed(() => data.value?.points.some((point) => point.file_count > 0));
const date = new Intl.DateTimeFormat('zh-CN', {
  month: '2-digit',
  day: '2-digit',
  hour: '2-digit',
  minute: '2-digit',
  hourCycle: 'h23',
});
const shortTime = new Intl.DateTimeFormat('zh-CN', {
  hour: '2-digit',
  minute: '2-digit',
  second: '2-digit',
  hourCycle: 'h23',
});

function span(point: DeliverySpeed['points'][number]) {
  return `${date.format(point.start_at * 1000)} – ${shortTime.format(point.end_at * 1000)}`;
}

function load(signal?: AbortSignal) {
  const scope = instances.scope();
  return history.load(
    (signal) => api.request<DeliverySpeed>('/api/workbench/delivery-speed', { scope, signal }),
    signal,
  );
}

usePolling(ref(true), load, 30000);
</script>

<template>
  <section class="card">
    <h2>近 24 小时平均交付速度</h2>
    <p class="muted">每 10 分钟统计正式交付文件的平均速度（Mbps），不足 10 分钟按实际时长计算。</p>
    <p v-if="history.error.value" class="form-error" role="alert">
      交付速度读取失败：{{ history.error.value }} <button @click="load()">重试</button>
    </p>
    <p v-else-if="!data" class="muted">正在读取交付速度…</p>
    <template v-if="data">
      <p v-if="!hasDeliveries" class="muted">近 24 小时暂无正式交付。</p>
      <div
        class="delivery-chart"
        role="img"
        aria-label="近 24 小时每 10 分钟平均交付速度，单位 Mbps"
      >
        <div class="delivery-scale" aria-hidden="true">
          <span>{{ maximum }}</span
          ><span>0</span>
        </div>
        <div class="delivery-bars">
          <div
            v-for="point in data.points"
            :key="point.start_at"
            class="delivery-column"
            :style="{ flexGrow: point.end_at - point.start_at }"
            :title="`${span(point)} · ${point.mbps} Mbps · ${point.file_count} 个文件`"
          >
            <div class="delivery-bar" :style="{ height: `${(point.mbps / maximum) * 100}%` }" />
          </div>
        </div>
        <div class="delivery-times muted" aria-hidden="true">
          <span>{{ date.format(data.start_at * 1000) }}</span>
          <span>{{ date.format(data.end_at * 1000) }}</span>
        </div>
      </div>
      <details @toggle="showDetails = ($event.target as HTMLDetailsElement).open">
        <summary>查看每 10 分钟交付明细</summary>
        <div v-if="showDetails" class="table-scroll delivery-details">
          <table>
            <thead>
              <tr>
                <th>时间段</th>
                <th>正式交付</th>
                <th>平均交付速度</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="point in [...data.points].reverse()" :key="point.start_at">
                <td>{{ span(point) }}{{ point.partial ? '（不足 10 分钟）' : '' }}</td>
                <td>{{ point.file_count }} 个 / {{ formatGB(point.file_bytes_exact) }}</td>
                <td>{{ point.mbps }} Mbps</td>
              </tr>
            </tbody>
          </table>
        </div>
      </details>
    </template>
  </section>
</template>

<style scoped>
.delivery-chart {
  display: grid;
  grid-template-columns: auto minmax(0, 1fr);
  gap: 4px 8px;
  margin: 12px 0;
}
.delivery-scale,
.delivery-times {
  display: flex;
  justify-content: space-between;
  font-size: 0.8rem;
}
.delivery-scale {
  flex-direction: column;
  text-align: right;
}
.delivery-bars {
  display: flex;
  height: 120px;
  border-block: 1px solid var(--line);
}
.delivery-column {
  display: flex;
  flex-basis: 0;
  min-width: 0;
  align-items: end;
}
.delivery-column:hover {
  background: var(--line);
}
.delivery-bar {
  width: 100%;
  background: var(--accent);
}
.delivery-times {
  grid-column: 2;
}
.delivery-details {
  max-height: 320px;
  overflow: auto;
}
</style>
