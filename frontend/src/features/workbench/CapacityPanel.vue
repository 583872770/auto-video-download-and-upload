<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from 'vue';
import type { Capacity } from './contracts';
import AccountTasks from './AccountTasks.vue';
import { sampleNow } from './waits';
const props = defineProps<{ status: Capacity; receivedAt?: number }>();
const capacity = computed(() => props.status.concurrency);
let sampledAt = performance.now();
const tick = ref(sampledAt);
const wallNow = ref(Date.now() / 1000);
const now = computed(() => sampleNow(capacity.value.time, sampledAt, tick.value));
watch(
  () => capacity.value.time,
  () => {
    sampledAt = performance.now();
    tick.value = sampledAt;
  },
);
const timer = setInterval(() => {
  tick.value = performance.now();
  wallNow.value = Date.now() / 1000;
}, 1000);
onUnmounted(() => clearInterval(timer));
const fresh = computed(
  () => capacity.value.online && (!props.receivedAt || wallNow.value - props.receivedAt <= 30),
);
const displayFresh = computed(
  () => capacity.value.online && (!props.receivedAt || wallNow.value - props.receivedAt <= 90),
);
const worker = computed(() =>
  displayFresh.value &&
  capacity.value.sample_fresh &&
  (!capacity.value.sample_time || now.value - capacity.value.sample_time <= 60)
    ? capacity.value.worker_control
    : undefined,
);
const sessions = computed(() => capacity.value.cookie_pool?.sessions || []);
function at(stamp?: number) {
  return stamp ? new Date(stamp * 1000).toLocaleString('zh-CN') : '—';
}
const actions: Record<string, string> = {
  halt: '停止并人工检查',
  cooldown: '临时冷却',
  wait: '等待启动窗口',
  observe: '恢复观察',
  record: '记录故障',
};
const blockers: Record<string, string> = {
  pressure: '资源压力仍在',
  cpu: 'CPU 未低于恢复阈值',
  io: 'I/O 压力未低于恢复阈值',
  memory: '可用内存未达到恢复水位',
};
</script>
<template>
  <section class="card capacity-panel" aria-label="当前实例传输并发">
    <div class="section-heading">
      <h2>执行容量与等待原因</h2>
      <span class="badge">账号池调度</span>
    </div>
    <p v-if="!fresh" class="notice">
      实例离线或采样尚未更新；下方保留上次记录，不能确认当前占用或恢复进度。
    </p>
    <div class="summary-grid">
      <div class="card">
        <small>下载进程 / 当前额度</small
        ><strong
          >{{ displayFresh ? (worker?.used ?? capacity.running) : '—' }} /
          {{ displayFresh ? (worker?.dynamic_limit ?? capacity.effective_limit) : '—' }}</strong
        >
      </div>
      <div class="card">
        <small>实际传输 / 传输上限</small
        ><strong
          >{{ displayFresh ? capacity.transferring : '—' }} / {{ capacity.transfer_limit }}</strong
        >
      </div>
      <div class="card">
        <small>单文件分片上限</small><strong>{{ capacity.fragment_limit }}</strong>
      </div>
      <div class="card">
        <small>集群下载 / 全局上限</small
        ><strong
          >{{ displayFresh ? capacity.global_running : '—' }} /
          {{ capacity.global_limit || '不额外限制' }}</strong
        >
      </div>
    </div>
    <p v-if="capacity.local_processing" class="notice">
      本地处理：{{ displayFresh ? capacity.local_processing.running : '—' }} /
      {{ capacity.local_processing.limit }} · 等待处理
      {{ displayFresh ? capacity.local_processing.queued : '—' }} 个任务。
      网络下载完成即释放账号，格式转换和文件交付使用独立工作池。
    </p>
    <p>{{ capacity.bottleneck }}</p>
    <p v-if="capacity.disk_message" class="notice">{{ capacity.disk_message }}</p>
    <template v-if="worker"
      ><p>
        常规进程上限 {{ worker.normal_limit }}，配置上限
        {{ worker.configured_limit || '不限制' }}，I/O 压力
        {{
          worker.measurements?.io_pressure == null
            ? '未取得'
            : worker.measurements.io_pressure + '%'
        }}{{
          worker.measurements?.io_pressure_source === 'host'
            ? '（整机回退采样）'
            : worker.measurements?.io_pressure_source === 'cgroup'
              ? '（本实例）'
              : ''
        }}。
      </p>
      <p v-if="worker.auto_reduce_enabled">
        自动降低下载进程上限已开启；缩容后，已有任务继续执行，完成后才释放名额。
      </p>
      <p v-else class="notice">
        自动降低下载进程上限已关闭；CPU、可用内存或 I/O 压力升高时不自动缩容。
        账号容量、内存容量预估、数据库连接容量与磁盘空间保护仍生效。
      </p>
      <p>
        CPU {{ worker.measurements?.cpu_percent ?? '—' }}% · 可用内存
        {{ worker.measurements?.memory_available_mb ?? '—' }} MB。
      </p>
      <div v-if="worker.resource_limited" class="notice">
        <p v-if="worker.last_reduction">
          最近一次缩容 {{ worker.last_reduction.from }} → {{ worker.last_reduction.to }} 个进程。
        </p>
        <template v-if="worker.recovery"
          ><p>
            恢复需同时满足 CPU &lt; {{ worker.recovery.cpu_below }}%、可用内存 ≥
            {{ worker.recovery.memory_at_least_mb }} MB{{
              worker.measurements?.io_pressure == null
                ? '；未取得 I/O 指标，按 CPU 和内存检查'
                : '、I/O 压力 < ' + worker.recovery.io_below + '%'
            }}。
          </p>
          <p>
            {{
              worker.recovery.blocked_by
                .map((code) => blockers[code] || '等待资源恢复')
                .join('；') || '等待新采样检查'
            }}
            · 下次可调整检查 {{ at(worker.recovery.eligible_at) }}。每次最多增加
            {{ worker.recovery.step }} 个，至少间隔
            {{ worker.recovery.interval_seconds }} 秒；时间到不保证扩容。
          </p></template
        >
      </div></template
    >
    <div v-for="risk in capacity.risks" :key="risk.code" class="notice">{{ risk.detail }}</div>
    <template v-if="capacity.decisions">
      <h3>等待与恢复依据</h3>
      <ul>
        <li v-for="(restriction, index) in capacity.decisions.restrictions" :key="index">
          {{ restriction.scope }} · {{ actions[restriction.action] || restriction.action }} ·
          {{ restriction.reason
          }}<template v-if="restriction.until"> · 检查时间 {{ at(restriction.until) }}</template
          ><template v-if="restriction.manual"> · 需人工检查</template>
        </li>
      </ul>
      <p>{{ capacity.decisions.combination }} {{ capacity.decisions.recovery.instructions }}</p>
      <details>
        <summary>最近触发事件与处理记录</summary>
        <div
          v-for="incident in capacity.decisions.history"
          :key="incident.incident_id"
          class="detail-row"
        >
          <span
            >{{ incident.failure?.kind }} · {{ incident.failure?.message
            }}<small class="cell-note">{{ at(incident.failure?.observed_at) }}</small></span
          ><span v-for="(effect, index) in incident.effects" :key="index"
            >{{ effect.scope }}：{{ actions[effect.action] || effect.action }}
            {{ at(effect.until) }}</span
          >
        </div>
        <p v-if="!capacity.decisions.history.length">暂无结构化事件记录。</p>
      </details>
    </template>
    <AccountTasks
      v-if="capacity.cookie_allocation || sessions.length"
      :accounts="capacity.cookie_allocation?.accounts || []"
      :sessions="sessions"
      :unassigned="capacity.cookie_allocation?.unassigned_tasks || []"
      :fresh="fresh"
      :time="capacity.time"
      :now="now"
      :pending="capacity.pending_tasks"
      :paused="capacity.paused"
    />
  </section>
</template>
