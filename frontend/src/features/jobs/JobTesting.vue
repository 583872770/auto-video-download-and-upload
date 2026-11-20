<script setup lang="ts">
import { computed, ref, watch } from 'vue';
import { RouterLink } from 'vue-router';
import { api } from '../../api';
import { bootstrap } from '../../bootstrap';
import { usePolling } from '../../composables/polling';
import { useResource } from '../../composables/resource';
import { useLeaveGuard } from '../../composables/leaveGuard';
import { jobEndpoint, formatGB, type Job } from './contracts';
import { expiry, reviewLabels, type SamplePolicy, type TestSample } from './results';
import VideoResults from './VideoResults.vue';
type Point = {
  time: number;
  succeeded: number;
  failed: number;
  http403: number;
  http429: number;
  auth: number;
  size: number;
  seconds: number;
};
interface Observation {
  totals: Record<string, number>;
  success_rate: number | null;
  points: Point[];
  details: Array<{
    id: string;
    task_id: string;
    video_id: string;
    instance_id: string;
    account_id: string;
    created: number;
    event: string;
    message: string;
    status: string;
  }>;
  accounts: Array<{ id: string; name: string }>;
  instances: Array<{ id: string; name: string }>;
  coverage: string;
}
const props = defineProps<{ job: Job; compact?: boolean }>();
const admin = bootstrap().role === 'admin';
const samples = useResource<{ items: TestSample[]; policy: SamplePolicy; note: string }>(),
  observation = useResource<Observation>();
const draft = ref<SamplePolicy>(),
  baseline = ref(''),
  saving = ref(false),
  error = ref(''),
  message = ref(''),
  viewing = ref<string>();
const hours = ref('24'),
  instance = ref(''),
  account = ref(''),
  event = ref('');
const dirty = computed(() => !!draft.value && JSON.stringify(draft.value) !== baseline.value);
useLeaveGuard(() => ({ dirty: dirty.value, busy: saving.value }));
async function refresh(signal?: AbortSignal) {
  await samples.load(
    (signal) => api.request(jobEndpoint(props.job.id) + '/samples', { signal }),
    signal,
  );
  if (!draft.value && samples.data.value) {
    draft.value = { ...samples.data.value.policy };
    baseline.value = JSON.stringify(draft.value);
  }
  if (!props.compact) {
    const params = new URLSearchParams({
      hours: hours.value,
      instance_id: instance.value,
      account_id: account.value,
      event: event.value,
    });
    await observation.load(
      (signal) => api.request(jobEndpoint(props.job.id) + '/observation?' + params, { signal }),
      signal,
    );
  }
}
usePolling(
  computed(() => !saving.value && !viewing.value),
  refresh,
  15000,
);
watch([hours, instance, account, event], () => {
  void refresh();
});
async function save() {
  if (!admin || !draft.value) return;
  saving.value = true;
  error.value = '';
  try {
    draft.value = await api.request<SamplePolicy>(jobEndpoint(props.job.id) + '/sample-policy', {
      method: 'PUT',
      body: draft.value,
    });
    baseline.value = JSON.stringify(draft.value);
    message.value = '设置已保存，后续测试按新设置整理；已有样本会在下一轮自动清理时检查。';
    await refresh();
  } catch (failure) {
    error.value = String(failure);
  } finally {
    saving.value = false;
  }
}
const labels: Record<string, string> = {
  http403: 'HTTP 403',
  http429: 'HTTP 429',
  auth: '登录 / 网页验证',
  local_error: '本地处理异常',
  other_error: '其他请求异常',
};
function name(options: Array<{ id: string; name: string }> | undefined, id: string) {
  return options?.find((o) => o.id === id)?.name || id || '未记录';
}
const chart = computed(() => {
  const points = observation.data.value?.points || [];
  const maximum = Math.max(
    1,
    ...points.map((p) => Math.max(p.succeeded + p.failed, p.http403 + p.http429 + p.auth)),
  );
  const path = (value: (p: Point) => number) =>
    points
      .map(
        (p, i) =>
          `${i ? 'L' : 'M'}${20 + (i * 660) / Math.max(1, points.length - 1)},${150 - (value(p) * 125) / maximum}`,
      )
      .join(' ');
  return {
    max: maximum,
    success: path((p) => p.succeeded),
    failure: path((p) => p.failed),
    incident: path((p) => p.http403 + p.http429 + p.auth),
    points,
  };
});
</script>
<template>
  <section class="card">
    <div class="section-heading">
      <div>
        <h2>测试样本留存</h2>
        <p>真实文件可核对，长期测试自动循环回收。</p>
      </div>
      <button @click="refresh()">刷新</button>
    </div>
    <p v-if="samples.error.value || error" class="form-error" role="alert">
      {{ error || samples.error.value }}
    </p>
    <p v-if="message" class="notice" role="status">{{ message }}</p>
    <template v-if="samples.data.value">
      <p class="notice">
        {{
          samples.data.value.policy.enabled
            ? `循环保留：每任务、每实例最多 ${samples.data.value.policy.maximum} 份 / ${samples.data.value.policy.budget_mb} MiB / ${samples.data.value.policy.hours} 小时，任一限制先到先回收。`
            : '快速测试：校验并登记结果后清理真实文件。需要核对时，可对某个视频单独重新下载并留样。'
        }}
      </p>
      <p>
        {{ samples.data.value.note }}
        正在下载、处理和查看的文件受保护。失败测试的待处理源文件默认保留
        {{ samples.data.value.policy.failure_hours }} 小时，空间紧张时可提前回收。
      </p>
      <RouterLink class="text-link" to="/settings#maintenance"
        >查看实例空间与全部样本容量上限 →</RouterLink
      >
      <details v-if="draft && !compact" class="sample-settings">
        <summary>修改留样设置{{ dirty ? ' · 有未保存修改' : '' }}</summary>
        <form @submit.prevent="save">
          <fieldset :disabled="!admin || saving">
            <label
              >测试完成后<select v-model="draft.enabled">
                <option :value="true">循环保留真实样本</option>
                <option :value="false">快速测试，校验后清理</option>
              </select></label
            >
            <div class="policy-grid">
              <label
                >每实例最多样本数<input
                  v-model.number="draft.maximum"
                  type="number"
                  min="1"
                  max="20"
                  required
              /></label>
              <label
                >每实例本任务上限（MiB）<input
                  v-model.number="draft.budget_mb"
                  type="number"
                  min="16"
                  max="102400"
                  required
              /></label>
              <label
                >成功样本保留（小时）<input
                  v-model.number="draft.hours"
                  type="number"
                  min="1"
                  max="168"
                  required
              /></label>
              <label
                >失败源文件最多保留（小时）<input
                  v-model.number="draft.failure_hours"
                  type="number"
                  min="1"
                  max="168"
                  required
              /></label>
            </div>
            <button class="primary" :disabled="saving || !dirty">保存留样设置</button>
          </fieldset>
        </form>
      </details>
      <div v-if="compact" class="toolbar">
        <button @click="$router.replace({ query: { tab: 'testing' } })">
          查看样本与测试观察 →
        </button>
      </div>
      <template v-else>
        <h3>当前保留样本（最近 200 份）</h3>
        <p v-if="!samples.data.value.items.length">
          当前没有留存样本。新测试完成后按上方规则自动留样；空间不足、超额或到期时会自动清理。
        </p>
        <div class="sample-list">
          <article v-for="sample in samples.data.value.items" :key="sample.id" class="sample-item">
            <div>
              <strong>{{ sample.title || sample.video_id }}</strong
              ><small class="cell-note"
                >{{ sample.video_id }} · {{ reviewLabels[sample.review] }}</small
              ><small class="cell-note"
                >{{ expiry(sample.expires) }} · {{ formatGB(sample.size) }} ·
                {{ sample.instance_id }}</small
              >
            </div>
            <button @click="viewing = sample.task_id">核对内容 / 验收</button>
          </article>
        </div>
      </template>
    </template>
  </section>
  <section v-if="!compact" class="card">
    <h2>测试观察</h2>
    <p>
      成功率按最终测试结果计算；请求异常次数单独统计。等待空间、账号间隔和正常冷却不计为测试失败。
    </p>
    <div class="toolbar">
      <select v-model="hours" aria-label="观察时间">
        <option value="1">近 1 小时</option>
        <option value="24">近 24 小时</option>
        <option value="168">近 7 天</option>
        <option value="720">近 30 天</option>
      </select>
      <select v-model="instance" aria-label="观察实例">
        <option value="">全部实例</option>
        <option v-for="o in observation.data.value?.instances" :key="o.id" :value="o.id">
          {{ o.name }}
        </option>
      </select>
      <select v-model="account" aria-label="观察账号">
        <option value="">全部账号</option>
        <option v-for="o in observation.data.value?.accounts" :key="o.id" :value="o.id">
          {{ o.name }}
        </option>
      </select>
    </div>
    <p v-if="observation.error.value" class="form-error" role="alert">
      {{ observation.error.value }}
    </p>
    <template v-if="observation.data.value">
      <div class="summary-grid">
        <div class="card">
          <small>最终成功率</small
          ><strong>{{
            observation.data.value.success_rate === null
              ? '暂无结果'
              : observation.data.value.success_rate + '%'
          }}</strong
          ><small
            >成功 {{ observation.data.value.totals.succeeded }} / 最终失败
            {{ observation.data.value.totals.failed }} 次</small
          >
        </div>
        <div class="card">
          <small>HTTP 403</small><strong>{{ observation.data.value.totals.http403 }}</strong
          ><small>请求被拒绝次数</small>
        </div>
        <div class="card">
          <small>HTTP 429</small><strong>{{ observation.data.value.totals.http429 }}</strong
          ><small>请求限流次数</small>
        </div>
        <div class="card">
          <small>登录 / 网页验证</small><strong>{{ observation.data.value.totals.auth }}</strong
          ><small>需要验证的请求次数</small>
        </div>
      </div>
      <figure class="test-chart">
        <figcaption>
          次数趋势 · 每格{{ hours === '720' ? ' 6 小时' : ' 1 小时' }} · 最高 {{ chart.max }} 次
        </figcaption>
        <svg
          viewBox="0 0 700 175"
          role="img"
          aria-label="成功、最终失败、403和429及验证异常次数趋势"
        >
          <line x1="20" y1="150" x2="680" y2="150" stroke="currentColor" opacity="0.2" />
          <path :d="chart.success" fill="none" stroke="#16836f" stroke-width="3" />
          <path :d="chart.failure" fill="none" stroke="#d35b4c" stroke-width="2" />
          <path
            :d="chart.incident"
            fill="none"
            stroke="#bf830c"
            stroke-width="2"
            stroke-dasharray="5 3"
          />
          <circle
            v-for="(p, i) in chart.points"
            :key="p.time"
            :cx="20 + (i * 660) / Math.max(1, chart.points.length - 1)"
            :cy="150 - (p.succeeded * 125) / chart.max"
            r="4"
            fill="transparent"
          >
            <title>
              {{ new Date(p.time * 1000).toLocaleString() }}：成功 {{ p.succeeded }}，失败
              {{ p.failed }}，403 {{ p.http403 }}，429 {{ p.http429 }}，验证 {{ p.auth }}
            </title>
          </circle>
        </svg>
        <div class="chart-legend">
          <span>🟢 最终成功</span><span>🔴 最终失败</span><span>🟠 403 / 429 / 验证</span>
        </div>
        <small v-if="chart.points.length"
          >{{ new Date(chart.points[0]!.time * 1000).toLocaleString() }} —
          {{ new Date(chart.points[chart.points.length - 1]!.time * 1000).toLocaleString() }}</small
        >
      </figure>
      <p>
        任务当前实时下载速度：{{ (job.transfer_speed / 1e6).toFixed(2) }}
        MB/s（全部实例，不随上述历史筛选变化）。
      </p>
      <p class="muted">{{ observation.data.value.coverage }}</p>
      <h3>请求异常明细（符合筛选的最近 100 条）</h3>
      <select v-model="event" aria-label="异常类型">
        <option value="">全部异常</option>
        <option v-for="(label, key) in labels" :key="key" :value="key">{{ label }}</option>
      </select>
      <div class="table-scroll">
        <table>
          <thead>
            <tr>
              <th>时间 / 类型</th>
              <th>视频 / 当前结果</th>
              <th>实例 / 账号</th>
              <th>原因</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="item in observation.data.value.details" :key="item.id">
              <td>
                {{ new Date(item.created * 1000).toLocaleString()
                }}<small class="cell-note">{{ labels[item.event] || item.event }}</small>
              </td>
              <td>
                <button @click="viewing = item.task_id">{{ item.video_id }}</button
                ><small class="cell-note"
                  >当前：{{
                    item.status === 'succeeded'
                      ? '已成功'
                      : item.status === 'queued'
                        ? '等待后续执行'
                        : item.status === 'failed'
                          ? '最终失败'
                          : item.status
                  }}</small
                >
              </td>
              <td>
                {{ name(observation.data.value.instances, item.instance_id)
                }}<small class="cell-note">{{
                  name(observation.data.value.accounts, item.account_id)
                }}</small>
              </td>
              <td>{{ item.message }}</td>
            </tr>
            <tr v-if="!observation.data.value.details.length">
              <td colspan="4" class="empty">此时间范围与筛选下暂无请求异常。</td>
            </tr>
          </tbody>
        </table>
      </div>
    </template>
  </section>
  <VideoResults
    v-if="viewing"
    :job-id="job.id"
    :task-id="viewing"
    @close="viewing = undefined"
    @changed="refresh()"
  />
</template>
<style scoped>
.sample-settings {
  margin: 20px 0;
}
.sample-settings summary {
  cursor: pointer;
}
.sample-settings label {
  display: grid;
  gap: 8px;
  margin: 12px 0;
}
.policy-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 16px;
}
.sample-list {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(min(100%, 320px), 1fr));
  gap: 12px;
}
.sample-item {
  padding: 16px;
  border: 1px solid var(--line);
  border-radius: 12px;
}
.sample-item button {
  margin-top: 12px;
}
.test-chart {
  margin: 24px 0;
}
.test-chart svg {
  width: 100%;
  min-height: 140px;
}
.chart-legend {
  display: flex;
  flex-wrap: wrap;
  gap: 18px;
  margin: 10px 0;
}
@media (max-width: 600px) {
  .policy-grid {
    grid-template-columns: 1fr;
    gap: 0;
  }
}
</style>
