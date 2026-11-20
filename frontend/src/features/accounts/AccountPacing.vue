<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue';
import { api } from '../../api';
import { useInstances } from '../../stores/instances';
import { useOperation } from '../../composables/operation';
import { usePolling } from '../../composables/polling';
import { useLeaveGuard } from '../../composables/leaveGuard';
import { confirmAction } from '../../composables/dialogs';
import { constraints, type ConsoleConfig } from '../../generated/config';
defineProps<{ admin: boolean }>();
const emit = defineEmits<{ pending: [value: boolean]; saved: [] }>();
const instances = useInstances(),
  op = useOperation();
const config = ref<ConsoleConfig>(),
  baseline = ref('');
const pacing = reactive<Record<string, string | number | boolean>>({});
const fields = [
  {
    key: 'youtube_cookie_pool_start_interval',
    label: '账号池共用启动间隔（秒）',
    help: '全部 Cookie 共用，0 关闭此间隔。新任务使用新值，已有等待窗口保留到期。',
  },
  {
    key: 'youtube_start_interval',
    label: '两次任务启动最小间隔（秒）',
    help: '账号池逐账号计算。预热、冷却、恢复保护可能延长实际间隔，降低配置不会立即解除保护。',
  },
  {
    key: 'youtube_task_rest_interval',
    label: '任务结束后休息（秒）',
    help: '成功及已发起请求的失败均生效，0 关闭；与启动间隔取更晚时间，不直接相加。',
  },
  {
    key: 'youtube_cookie_session_limit',
    label: '每 Cookie 会话任务上限',
    help: '预热和恢复期仍限制为 1 个，实际进程另受内存、数据库及进程上限约束。',
  },
  {
    key: 'youtube_hourly_start_limit',
    label: '滚动一小时启动预算',
    help: '0 不限制；逐账号生效，视频与重试共用额度。',
  },
  {
    key: 'youtube_daily_start_limit',
    label: '滚动二十四小时启动预算',
    help: '0 不限制；保存节奏、重启或重新导入不会清空账号池预算记录。',
  },
  {
    key: 'youtube_sleep_preset',
    label: 'yt-dlp sleep 预设',
    options: [
      [true, '启用（推荐）'],
      [false, '关闭'],
    ],
    help: '解析间隔 0.75 秒，视频、音频和字幕文件开始前统一等待 10～20 秒，不限制已开始的字节传输。',
  },
  {
    key: 'youtube_throttle_cooldown',
    label: 'HTTP 429 最短冷却（秒）',
    help: '源站 Retry-After 更长时采用更长等待，到期仍需单路恢复观察。',
  },
];
const dirty = computed(() => !!baseline.value && JSON.stringify(pacing) !== baseline.value);
watch([dirty, op.busy], () => emit('pending', dirty.value || op.busy.value), { immediate: true });
useLeaveGuard(() => ({ dirty: dirty.value, busy: op.busy.value }));
function range(key: string) {
  return constraints[key as keyof typeof constraints];
}
function update(key: string, event: Event) {
  const raw = (event.target as HTMLSelectElement).value;
  const option = fields
    .find((field) => field.key === key)
    ?.options?.find(([value]) => String(value) === raw);
  pacing[key] = option ? option[0]! : Number(raw);
}
async function load() {
  const { config: value } = await api.request<{ config: ConsoleConfig }>('/api/config', {
    scope: instances.scope(),
  });
  config.value = value;
  if (!dirty.value) {
    for (const field of fields)
      pacing[field.key] = value[field.key as keyof ConsoleConfig] as string | number | boolean;
    baseline.value = JSON.stringify(pacing);
  }
}
async function discard() {
  if (dirty.value && !(await confirmAction('放弃未保存的节奏修改？', '有未保存的修改'))) return;
  baseline.value = '';
  await op.run(load);
}
async function save() {
  await op.run(async () => {
    const body = Object.fromEntries(
      Object.entries(pacing).filter(([key, value]) => JSON.parse(baseline.value)[key] !== value),
    );
    const scope = instances.scope();
    await api.request('/api/config', { method: 'PUT', scope, body });
    const { config: value } = await api.request<{ config: ConsoleConfig }>('/api/config', {
      scope,
    });
    if (
      Object.entries(body).some(([key, expected]) => value[key as keyof ConsoleConfig] !== expected)
    )
      throw new Error('服务器读回与提交值不同，草稿已保留。');
    config.value = value;
    baseline.value = JSON.stringify(pacing);
    op.message.value = `「${instances.name}」请求节奏已保存，作用于本实例的后续任务；账号冷却保持有效。`;
    emit('saved');
  });
}
usePolling(
  computed(() => !config.value && !op.busy.value),
  async () => {
    await op.run(load);
  },
  10000,
);
</script>
<template>
  <section class="card account-pacing">
    <div class="page-heading">
      <div>
        <h2>{{ instances.name }} · 请求节奏</h2>
        <p>配置本实例的任务并发、启动间隔和限流保护。账号在“账号清单”统一管理。</p>
      </div>
      <button :disabled="op.busy.value" @click="discard">
        {{ dirty ? '放弃修改并重新读取' : '重新读取' }}
      </button>
    </div>
    <p v-if="op.error.value" class="notice error" role="alert">{{ op.error.value }}</p>
    <p v-if="op.message.value" class="notice" role="status">{{ op.message.value }}</p>
    <p v-if="!config" role="status">正在读取实例配置…</p>
    <template v-else>
      <form @submit.prevent="save">
        <fieldset :disabled="!admin || op.busy.value">
          <legend>任务节奏与启动预算</legend>
          <div class="form-grid">
            <label v-for="field in fields" :key="field.key"
              >{{ field.label
              }}<select
                v-if="field.options"
                :value="String(pacing[field.key])"
                @change="update(field.key, $event)"
              >
                <option
                  v-for="[value, label] in field.options"
                  :key="String(value)"
                  :value="String(value)"
                >
                  {{ label }}
                </option></select
              ><input
                v-else
                :value="pacing[field.key]"
                type="number"
                :min="range(field.key)?.[0]"
                :max="range(field.key)?.[1]"
                required
                @input="update(field.key, $event)"
              /><small>{{ field.help }}</small></label
            >
          </div>
          <p class="muted">
            可归属的限流只冷却该账号；一天内重复限流按 1 / 2 / 4 / 8 小时递增。到期自动单路、至少
            300 秒恢复，需同时满足有效成功与观察时间；普通网络错误不重置账号恢复进度。
          </p>
          <button :disabled="!dirty">保存节奏</button>
        </fieldset>
      </form>
    </template>
  </section>
</template>
