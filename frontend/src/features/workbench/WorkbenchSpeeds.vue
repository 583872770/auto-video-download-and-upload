<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from 'vue';
import type { WorkbenchStatus } from './contracts';
import { speedNumber } from './speeds';
import { formatGB } from '../jobs/contracts';

const props = defineProps<{
  status?: WorkbenchStatus;
  receivedAt?: number;
}>();
const FRESH_SECONDS = 30;
const DISPLAY_STALE_SECONDS = 90;
const now = ref(Date.now() / 1000);
const receivedAt = ref(0);
const lastOutput = ref<number | null>(null);
const lastSessionStartedAt = ref('');
watch(
  () => props.status,
  (value) => {
    receivedAt.value = value ? Date.now() / 1000 : 0;
    if (!value) {
      lastOutput.value = null;
      lastSessionStartedAt.value = '';
      return;
    }
    const sessionStartedAt = value.session?.started_at || '';
    if (sessionStartedAt !== lastSessionStartedAt.value) {
      lastSessionStartedAt.value = sessionStartedAt;
      lastOutput.value = null;
    }
    if (value.session?.output_mbps != null && Number.isFinite(value.session.output_mbps))
      lastOutput.value = value.session.output_mbps;
  },
  { immediate: true },
);
const timer = setInterval(() => {
  now.value = Date.now() / 1000;
}, 1000);
onUnmounted(() => clearInterval(timer));
const transfer = computed(
  () => props.status?.transfer_summary || { time: 0, transferring: 0, measured: 0, speed: 0 },
);
const snapshotAt = computed(() => props.receivedAt || receivedAt.value);
const fresh = computed(
  () => !!props.status?.online && now.value - snapshotAt.value <= FRESH_SECONDS,
);
const displayFresh = computed(
  () => !!props.status?.online && now.value - snapshotAt.value <= DISPLAY_STALE_SECONDS,
);
const total = computed(() =>
  displayFresh.value
    ? transfer.value.transferring && !transfer.value.measured
      ? 0
      : transfer.value.speed
    : null,
);
const session = computed(() => props.status?.session);
const output = computed(() => {
  if (!displayFresh.value || !session.value) return null;
  return session.value.output_mbps ?? lastOutput.value ?? 0;
});
const readState = computed(() =>
  !props.status
    ? '正在读取当前实例'
    : !props.status.online
      ? '实例离线'
      : displayFresh.value && !fresh.value
        ? '数据稍旧，正在更新'
        : '采样过期，等待更新',
);
</script>

<template>
  <section class="workbench-speeds" aria-label="下载与产出速度">
    <article class="speed-metric" aria-labelledby="total-download-speed-label">
      <div class="speed-metric-heading">
        <h2 id="total-download-speed-label">实时总下载速度</h2>
        <span v-if="fresh" class="speed-indicator">{{ transfer.transferring }} 个传输任务</span>
        <span v-else-if="displayFresh" class="speed-indicator muted">数据稍旧</span>
        <span v-else class="speed-indicator muted">待更新</span>
      </div>
      <div class="speed-reading">
        <strong id="workbench-download-speed">{{
          speedNumber(total == null ? null : total / 1e6)
        }}</strong>
        <span>MB/s</span>
        <small>{{ speedNumber(total == null ? null : (total * 8) / 1e6) }} Mbps</small>
      </div>
      <p v-if="!displayFresh">{{ readState }}</p>
      <p v-else-if="!fresh">{{ readState }}</p>
      <p v-else-if="transfer.transferring && transfer.measured < transfer.transferring">
        当前实例 · {{ transfer.measured }} / {{ transfer.transferring }} 个任务有新鲜速度采样{{
          transfer.measured ? '，显示已取得的合计' : ''
        }}
      </p>
      <p v-else>当前实例全部传输任务合计 · 每 5 秒更新</p>
    </article>
    <article class="speed-metric output-speed-metric" aria-labelledby="session-output-speed-label">
      <div class="speed-metric-heading">
        <h2 id="session-output-speed-label">当前会话产出速度</h2>
        <span class="speed-indicator" :class="{ muted: !fresh || !session }">会话平均</span>
      </div>
      <div class="speed-reading">
        <strong id="workbench-output-speed">{{
          speedNumber(output == null ? null : output / 8)
        }}</strong>
        <span>MB/s</span>
        <small>{{ speedNumber(output) }} Mbps</small>
      </div>
      <p v-if="!displayFresh">{{ readState }}</p>
      <p v-else-if="!fresh">{{ readState }}</p>
      <p v-else-if="!session">暂无当前会话，启动下载后统计</p>
      <p v-else-if="output == null">会话刚开始，等待有效统计时长</p>
      <p v-else>完成文件量 ÷ 本会话时长 · 含测试产出</p>
      <small v-if="displayFresh && session" class="speed-session-total"
        >本会话已产出 {{ formatGB(session.downloaded_file_bytes_exact) }}</small
      >
    </article>
  </section>
</template>

<style scoped>
.workbench-speeds {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 12px;
  margin-bottom: 14px;
}
.speed-metric {
  padding: 15px 18px;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: white;
  min-width: 0;
}
.speed-metric-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 6px 12px;
}
.speed-metric h2 {
  font-size: 12px;
  font-weight: 500;
  margin: 0;
  color: #52657e;
}
.speed-indicator {
  color: #398775;
  background: #edf8f5;
  border-radius: 4px;
  padding: 2px 7px;
  font-size: 10px;
}
.output-speed-metric .speed-indicator {
  color: #4275bf;
  background: #eff6ff;
}
.speed-indicator.muted,
.output-speed-metric .speed-indicator.muted {
  color: #64748b;
  background: #f1f5f9;
}
.speed-reading {
  display: flex;
  align-items: baseline;
  flex-wrap: wrap;
  gap: 8px;
  margin: 7px 0 4px;
  font-variant-numeric: tabular-nums;
}
.speed-reading strong {
  font-size: 30px;
  line-height: 1.3;
  font-weight: 600;
  letter-spacing: -0.6px;
  color: #1e293b;
}
.speed-reading > span {
  font-size: 12px;
  color: #52657e;
}
.speed-reading > small {
  font-size: 11px;
  color: #64748b;
  padding-left: 10px;
  border-left: 1px solid var(--line);
}
.speed-metric p {
  font-size: 11px;
  color: #64748b;
  margin: 4px 0 0;
}
.speed-session-total {
  display: block;
  font-size: 10px;
  color: #7d8da2;
  margin-top: 3px;
}
@media (max-width: 560px) {
  .workbench-speeds {
    gap: 8px;
  }
  .speed-metric {
    padding: 12px;
  }
  .speed-metric-heading {
    align-items: flex-start;
    gap: 5px;
  }
  .speed-metric h2 {
    width: 100%;
    font-size: 11px;
  }
  .speed-reading {
    gap: 4px;
    margin-top: 8px;
  }
  .speed-reading strong {
    font-size: 24px;
  }
  .speed-reading > span {
    font-size: 10px;
  }
  .speed-reading > small {
    width: 100%;
    border: 0;
    padding: 0;
    font-size: 10px;
  }
  .speed-metric p {
    font-size: 10px;
  }
}
</style>
