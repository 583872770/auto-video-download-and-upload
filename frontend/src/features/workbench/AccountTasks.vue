<script setup lang="ts">
import { computed, ref, watch } from 'vue';
import { useInstances } from '../../stores/instances';
import TaskLog from '../../components/TaskLog.vue';
import { stageLabel, type AccountTask, type RuntimeAccount, type RuntimeSlot } from './contracts';
import { isWaitingStage, summarizeWaits, taskWait, taskWaitGroups, waitCountdown } from './waits';

const props = defineProps<{
  accounts: RuntimeAccount[];
  sessions: RuntimeSlot[];
  unassigned: AccountTask[];
  fresh: boolean;
  time: number;
  now: number;
}>();
const instances = useInstances();
function countdown(until?: number) {
  return waitCountdown(until, props.now, props.fresh);
}
const query = ref('');
const state = ref('');
const stage = ref('');
const page = ref(1);
const pageSize = 8;
const expanded = ref<string[]>([]);
const log = ref<{ id: string; instance_id: string }>();
const modes: Record<string, string> = {
  ready: '可调度',
  recovery: '恢复观察',
  cooldown: '冷却中',
  manual: '需人工处理',
  unavailable: '不可用',
  unknown: '状态待确认',
};
const mainStages = [
  { key: 'downloading', label: '传输中' },
  { key: 'extracting', label: '解析视频' },
  { key: 'waiting_transfer', label: '等待传输' },
  { key: 'processing', label: '合并 / 后处理' },
  { key: 'waiting_request', label: '等待请求间隔' },
  { key: 'uploading_oss', label: '上传 OSS' },
];
function matches(task: AccountTask) {
  return (
    !stage.value ||
    (stage.value === 'other'
      ? !mainStages.some((item) => item.key === task.stage)
      : task.stage === stage.value)
  );
}
function accountState(account: RuntimeAccount) {
  return props.fresh && account.known ? account.state || 'unknown' : 'unknown';
}
const sessionsById = computed(
  () => new Map(props.sessions.map((session) => [session.id, session])),
);
const allAccounts = computed<RuntimeAccount[]>(() => {
  const ids = new Set(props.accounts.map((account) => account.id));
  return [
    ...props.accounts,
    ...props.sessions
      .filter((session) => !ids.has(session.id))
      .map((session) => ({ ...session, tasks: [] })),
  ];
});
const matchingQuery = computed(() => {
  const needle = query.value.trim().toLowerCase();
  return allAccounts.value.filter(
    (account) => !needle || `${account.name} ${account.email || ''}`.toLowerCase().includes(needle),
  );
});
const searched = computed(() =>
  matchingQuery.value.filter((account) => !state.value || accountState(account) === state.value),
);
const counts = computed(() => {
  const result: Record<string, number> = { '': 0, other: 0 };
  for (const account of searched.value) {
    for (const task of account.tasks) {
      result['']!++;
      const key = mainStages.some((item) => item.key === task.stage) ? task.stage : 'other';
      result[key] = (result[key] || 0) + 1;
    }
  }
  return result;
});
const tabs = computed(() =>
  [{ key: '', label: '全部任务' }, ...mainStages, { key: 'other', label: '其他阶段' }].map(
    (item) => ({ ...item, count: counts.value[item.key] || 0 }),
  ),
);
const filtered = computed(() =>
  searched.value.filter((account) => !stage.value || account.tasks.some(matches)),
);
const waitingSummary = computed(() =>
  summarizeWaits(searched.value.flatMap((account) => account.tasks.filter(matches))),
);
const pages = computed(() => Math.max(1, Math.ceil(filtered.value.length / pageSize)));
const visible = computed(() =>
  filtered.value.slice((page.value - 1) * pageSize, page.value * pageSize),
);
const first = computed(() => (filtered.value.length ? (page.value - 1) * pageSize + 1 : 0));
const last = computed(() => Math.min(page.value * pageSize, filtered.value.length));
const pageNumbers = computed(() => {
  const start = Math.max(1, Math.min(page.value - 2, pages.value - 4));
  return Array.from({ length: Math.min(5, pages.value) }, (_, index) => start + index);
});
const totals = computed(() => {
  const result = { running: 0, waiting: 0, cooldown: 0, idle: 0 };
  for (const account of matchingQuery.value) {
    if (!account.known) continue;
    if (accountState(account) === 'cooldown') {
      result.cooldown++;
      continue;
    }
    if (account.tasks.some((task) => !isWaitingStage(task.stage))) result.running++;
    else if (
      account.tasks.length ||
      account.reasons?.some((reason) => reason.code !== 'unassigned') ||
      account.state !== 'ready'
    )
      result.waiting++;
    else result.idle++;
  }
  return result;
});
watch([query, state, stage], () => {
  page.value = 1;
});
watch(pages, (value) => {
  page.value = Math.min(page.value, value);
});
watch(
  () => instances.selectedId,
  () => {
    expanded.value = [];
    log.value = undefined;
    query.value = state.value = stage.value = '';
    page.value = 1;
  },
);
watch(
  () => props.fresh,
  (value) => {
    if (!value) log.value = undefined;
  },
);
function tasksFor(account: RuntimeAccount) {
  return account.tasks.filter(matches);
}
const waitsByAccount = computed(
  () =>
    new Map(allAccounts.value.map((account) => [account.id, taskWaitGroups(tasksFor(account))])),
);
function filterCooldown() {
  state.value = state.value === 'cooldown' ? '' : 'cooldown';
  stage.value = '';
}
function stagesFor(account: RuntimeAccount) {
  return [...new Set(tasksFor(account).map((task) => task.stage))];
}
function toggle(id: string) {
  expanded.value = expanded.value.includes(id)
    ? expanded.value.filter((value) => value !== id)
    : [...expanded.value, id];
}
function openDetails(id: string) {
  if (!expanded.value.includes(id)) expanded.value = [...expanded.value, id];
}
function openLog(task: AccountTask) {
  if (props.fresh) log.value = { id: task.task_id, instance_id: instances.selectedId };
}
function at(stamp?: number) {
  return stamp ? new Date(stamp * 1000).toLocaleString('zh-CN') : '—';
}
function speed(value?: number) {
  if (value == null || !Number.isFinite(value) || value < 0) return '';
  if (value < 1024) return `${value.toFixed(0)} B/s`;
  return value < 1024 ** 2
    ? `${(value / 1024).toFixed(1)} KB/s`
    : `${(value / 1024 ** 2).toFixed(1)} MB/s`;
}
function stageTone(value: string) {
  if (value === 'downloading') return 'blue';
  if (value === 'extracting') return 'purple';
  if (value === 'processing' || value === 'uploading_oss') return 'teal';
  return isWaitingStage(value) ? 'amber' : 'gray';
}
function pacing(account: RuntimeAccount) {
  return sessionsById.value.get(account.id)?.pacing || account.pacing;
}
function reasons(account: RuntimeAccount): NonNullable<RuntimeAccount['reasons']> {
  const priority = [
    'account_halted',
    'unavailable',
    'pool_halted',
    'account_cooldown',
    'pool_cooldown',
  ];
  return [
    ...taskWaitGroups(tasksFor(account)).map((reason) => ({
      ...reason,
      code: 'task_' + reason.code,
    })),
    ...(account.reasons || []),
  ].sort((a, b) => {
    const rank = (code: string) =>
      code.startsWith('task_')
        ? -1
        : priority.includes(code)
          ? priority.indexOf(code)
          : priority.length;
    return rank(a.code) - rank(b.code);
  });
}
const accountLink = computed(
  () =>
    '/accounts' +
    (instances.selectedId ? '?instance_id=' + encodeURIComponent(instances.selectedId) : ''),
);
</script>

<template>
  <section class="account-tasks" aria-label="账号任务与等待">
    <header class="account-panel-heading">
      <div class="account-panel-title">
        <h3>账号任务与等待</h3>
        <span
          >{{ searched.length }} 个账号 · {{ counts[''] }} 个{{
            fresh ? '执行任务' : '任务记录'
          }}</span
        >
      </div>
      <small class="account-sample" :class="{ stale: !fresh }">
        {{ fresh ? '采样更新' : '上次记录' }} · {{ at(time) }}
      </small>
    </header>
    <div class="account-toolbar">
      <input
        v-model="query"
        type="search"
        aria-label="搜索运行账号"
        placeholder="搜索账号名称或邮箱"
      />
      <select v-model="state" aria-label="账号运行状态">
        <option value="">全部账号状态</option>
        <option v-for="(label, key) in modes" :key="key" :value="key">{{ label }}</option>
      </select>
      <div class="account-totals" aria-label="账号状态统计">
        <span>
          执行中 <b>{{ fresh ? totals.running : '—' }}</b>
        </span>
        <span>
          等待中 <b>{{ fresh ? totals.waiting : '—' }}</b>
        </span>
        <button
          type="button"
          class="cooldown-stat"
          :class="{ active: state === 'cooldown' }"
          :aria-pressed="state === 'cooldown'"
          :disabled="!fresh"
          aria-label="筛选冷却中的账号"
          title="点击筛选冷却账号，再次点击显示全部状态"
          @click="filterCooldown"
        >
          冷却中 <b>{{ fresh ? totals.cooldown : '—' }}</b>
        </button>
        <span>
          空闲 <b>{{ fresh ? totals.idle : '—' }}</b>
        </span>
      </div>
    </div>
    <div class="account-stage-filter" role="group" aria-label="筛选执行阶段">
      <span class="account-stage-label">执行阶段</span>
      <button
        v-for="tab in tabs"
        :key="tab.key"
        type="button"
        class="account-stage-tab"
        :class="{ active: stage === tab.key }"
        :aria-pressed="stage === tab.key"
        :data-stage="tab.key || 'all'"
        @click="stage = tab.key"
      >
        {{ tab.label }} <span class="account-stage-count">{{ tab.count }}</span>
      </button>
    </div>
    <p v-if="!fresh" class="account-filter-note">
      当前状态待确认；阶段与数量为上次记录，恢复进度与传输速度暂不展示。
    </p>
    <p v-if="stage" class="account-filter-note" aria-live="polite">
      当前筛选：<b>{{ tabs.find((tab) => tab.key === stage)?.label }}</b
      >，共 <b>{{ counts[stage] || 0 }}</b> 个{{ fresh ? '任务' : '任务记录' }}，分布在
      <b>{{ filtered.length }}</b> 个账号。
      <button type="button" class="account-text-button" @click="stage = ''">清除阶段筛选</button>
    </p>
    <div v-if="waitingSummary.length" class="account-wait-summary" aria-label="任务等待原因汇总">
      <span>{{ fresh ? '正在等待' : '上次等待记录' }}</span>
      <span v-for="reason in waitingSummary" :key="reason.code" class="account-wait-chip">
        {{ reason.label }} <b>{{ reason.count }}</b>
      </span>
      <small>按当前筛选的任务统计</small>
    </div>
    <div class="account-columns" aria-hidden="true">
      <span>账号 / 状态</span><span>执行阶段</span><span>当前任务</span><span>并发 / 间隔</span
      ><span>等待 / 恢复</span><span>操作</span>
    </div>
    <div class="account-runtime-list" role="list" aria-label="账号运行列表">
      <article
        v-for="(account, index) in visible"
        :key="account.id"
        role="listitem"
        class="account-runtime-item"
        :data-account-id="account.id"
      >
        <div class="account-runtime-row">
          <div class="runtime-identity">
            <span class="runtime-avatar" aria-hidden="true">{{
              String(first + index).padStart(2, '0')
            }}</span>
            <div class="runtime-identity-copy">
              <h4 :title="account.name">{{ account.name }}</h4>
              <div class="runtime-secondary">
                <span class="runtime-state" :class="accountState(account)">{{
                  modes[accountState(account)] || '状态待确认'
                }}</span>
                <span
                  >·
                  {{
                    account.tasks.length
                      ? account.tasks.length + (fresh ? ' 个任务' : ' 条任务记录')
                      : '暂无任务'
                  }}</span
                >
              </div>
            </div>
          </div>
          <div class="runtime-stages" aria-label="执行阶段">
            <span
              v-for="value in stagesFor(account).slice(0, 2)"
              :key="value"
              class="runtime-stage"
              :class="fresh ? stageTone(value) : 'gray'"
              >{{ stageLabel(value) }}</span
            >
            <button
              v-if="stagesFor(account).length > 2"
              type="button"
              class="account-text-button"
              @click="openDetails(account.id)"
            >
              +{{ stagesFor(account).length - 2 }}
            </button>
            <span v-if="!tasksFor(account).length" class="runtime-stage gray">暂无任务</span>
          </div>
          <div class="runtime-task" :class="{ 'is-empty': !tasksFor(account).length }">
            <div class="runtime-task-title" :title="tasksFor(account)[0]?.title">
              {{ tasksFor(account)[0]?.title || '—' }}
            </div>
            <div class="runtime-secondary">
              <span
                v-if="
                  fresh &&
                  tasksFor(account)[0]?.stage === 'downloading' &&
                  speed(tasksFor(account)[0]?.speed)
                "
                class="runtime-speed"
                >↓ {{ speed(tasksFor(account)[0]?.speed) }}</span
              >
              <span v-else-if="!tasksFor(account).length">{{
                !fresh
                  ? '上次记录无任务'
                  : accountState(account) === 'ready'
                    ? '等待分配任务'
                    : '按账号状态检查'
              }}</span>
              <span v-else-if="!fresh">上次任务记录</span>
              <button
                v-if="tasksFor(account).length > 1"
                type="button"
                class="account-text-button"
                @click="openDetails(account.id)"
              >
                另 {{ tasksFor(account).length - 1 }} 个任务 ↗
              </button>
            </div>
          </div>
          <div class="runtime-quota">
            <div>
              <span class="account-mobile-label">并发 </span
              ><b>{{ fresh && account.known ? (account.active ?? '—') : '—' }}</b
              ><span class="runtime-divider"> / </span>{{ account.limit ?? '—' }}
              <span
                v-if="fresh && account.known && account.limit"
                class="runtime-meter"
                aria-hidden="true"
                ><i
                  v-for="n in Math.min(5, account.limit)"
                  :key="n"
                  :class="{
                    on:
                      n <=
                      Math.ceil(
                        (Math.min(account.active || 0, account.limit) / account.limit) *
                          Math.min(5, account.limit),
                      ),
                  }"
                ></i
              ></span>
            </div>
            <small>间隔 {{ account.interval ?? '—' }} 秒</small>
          </div>
          <div class="runtime-wait" :class="accountState(account)">
            <template v-if="!fresh || (!account.known && !waitsByAccount.get(account.id)?.length)"
              ><span>当前状态待确认</span><small>等待新鲜采样</small></template
            >
            <template v-else-if="waitsByAccount.get(account.id)?.length || account.reasons?.length">
              <div
                v-for="(reason, reasonIndex) in waitsByAccount.get(account.id)"
                :key="'task-' + reasonIndex"
                class="runtime-wait-entry"
                data-wait-kind="task"
              >
                <span class="runtime-reason"
                  >{{ reason.label }}
                  <span v-if="account.tasks.length > 1" class="runtime-wait-count"
                    >{{ reason.count }} 个任务</span
                  >
                </span>
                <small
                  v-if="
                    reason.detail &&
                    ['disk_wait', 'scheduling_wait', 'retrying', 'waiting_verification'].includes(
                      reason.code,
                    )
                  "
                  class="runtime-wait-detail"
                  >{{ reason.detail }}</small
                >
                <small
                  class="runtime-countdown"
                  :class="{ timed: !!reason.until }"
                  :title="reason.until ? '检查时间 ' + at(reason.until) : undefined"
                  >{{ countdown(reason.until) }}</small
                >
              </div>
              <div v-if="account.known && account.reasons?.length" class="runtime-account-waits">
                <small v-if="account.tasks.length" class="runtime-wait-caption">新任务准入</small>
                <div
                  v-for="reason in account.reasons"
                  :key="reason.code"
                  class="runtime-wait-entry"
                  data-wait-kind="account"
                >
                  <span class="runtime-reason">{{ reason.label }}</span>
                  <small
                    class="runtime-countdown"
                    :class="{ timed: !!reason.until }"
                    :title="reason.until ? '检查时间 ' + at(reason.until) : undefined"
                    >{{ countdown(reason.until) }}</small
                  >
                </div>
              </div>
            </template>
            <template v-else-if="pacing(account)?.recovery"
              ><span>低频恢复观察中</span
              ><small
                >有效成功 {{ pacing(account)?.success_count }} /
                {{ pacing(account)?.success_target }}</small
              ></template
            >
            <span v-else class="runtime-muted">无等待记录</span>
          </div>
          <div class="runtime-actions">
            <button
              v-if="tasksFor(account)[0]"
              type="button"
              class="account-text-button"
              :disabled="!fresh"
              :aria-label="'查看 ' + tasksFor(account)[0]!.title + ' 的日志'"
              @click="openLog(tasksFor(account)[0]!)"
            >
              日志
            </button>
            <button
              type="button"
              class="account-text-button runtime-details-toggle"
              :aria-expanded="expanded.includes(account.id)"
              :aria-controls="'runtime-details-' + account.id"
              :aria-label="account.name + ' 详情'"
              @click="toggle(account.id)"
            >
              详情 <span aria-hidden="true">{{ expanded.includes(account.id) ? '⌃' : '⌄' }}</span>
            </button>
          </div>
        </div>
        <div
          v-if="expanded.includes(account.id)"
          :id="'runtime-details-' + account.id"
          class="runtime-details"
        >
          <section class="runtime-detail-tasks">
            <h4>{{ fresh ? '全部任务' : '上次任务记录' }} · {{ account.tasks.length }}</h4>
            <div
              v-for="task in account.tasks"
              :key="task.task_id"
              class="runtime-detail-task"
              :data-account-task="task.task_id"
            >
              <span class="runtime-stage" :class="fresh ? stageTone(task.stage) : 'gray'">{{
                stageLabel(task.stage)
              }}</span>
              <span class="runtime-detail-task-name"
                >{{ task.title
                }}<small v-if="taskWait(task)" class="runtime-task-wait">
                  {{ fresh ? '等待：' : '上次等待：' }}{{ taskWait(task)?.label }}
                  <template v-if="taskWait(task)?.detail"> · {{ taskWait(task)?.detail }}</template>
                  <template v-if="fresh">
                    · {{ countdown(taskWait(task)?.until) }}</template
                  > </small
                ><small>{{
                  fresh
                    ? task.transfer_reserved
                      ? '已占用传输名额'
                      : '未占用传输名额'
                    : '当前传输占用待确认'
                }}</small></span
              >
              <span v-if="fresh && task.stage === 'downloading'" class="runtime-speed">{{
                speed(task.speed)
              }}</span>
              <button
                type="button"
                class="account-text-button"
                :disabled="!fresh"
                :aria-label="'查看 ' + task.title + ' 的日志'"
                @click="openLog(task)"
              >
                日志
              </button>
            </div>
            <p v-if="!account.tasks.length" class="runtime-muted">暂无执行任务。</p>
          </section>
          <section>
            <h4>等待与恢复依据{{ fresh && account.known ? '' : ' · 上次记录' }}</h4>
            <ul v-if="reasons(account).length" class="runtime-reasons">
              <li v-for="(reason, reasonIndex) in reasons(account)" :key="reasonIndex">
                {{ reason.label }}<small v-if="reason.detail">{{ reason.detail }}</small
                ><small v-if="fresh"
                  >{{ countdown(reason.until) }}；到期后仍需满足其他准入条件。</small
                >
              </li>
            </ul>
            <p v-else class="runtime-muted">暂无等待记录。</p>
            <template v-if="fresh && account.known && pacing(account)">
              <p>
                {{
                  pacing(account)!.recovery
                    ? '低频恢复观察'
                    : pacing(account)!.target_reached
                      ? '已达到设置目标'
                      : '稳定成功后逐步提速'
                }}；当前间隔至少 {{ pacing(account)!.interval }} 秒，配置下限
                {{ pacing(account)!.configured_minimum }} 秒。
              </p>
              <p>
                有效成功 {{ pacing(account)!.success_count }} /
                {{ pacing(account)!.success_target }} 次；观察
                {{ Math.floor(pacing(account)!.observed_seconds) }} /
                {{ pacing(account)!.observation_seconds }} 秒。
              </p>
              <p>
                两项门槛都满足后的有效成功，才重新检查下一档
                {{ pacing(account)!.next_interval }} 秒；时间到不会独立触发调整。
              </p>
              <p v-if="pacing(account)!.cooling_until">
                冷却检查时间 {{ at(pacing(account)!.cooling_until) }}；到期后仍需满足其他准入条件。
              </p>
            </template>
            <p v-if="sessionsById.get(account.id)">
              当前额度 {{ sessionsById.get(account.id)!.limit }} / 配置
              {{ sessionsById.get(account.id)!.configured_limit }}
            </p>
            <a class="text-link" :href="accountLink">账号检测与节奏设置 ↗</a>
          </section>
        </div>
      </article>
    </div>
    <p v-if="!filtered.length" class="account-empty">
      {{ allAccounts.length ? '暂无符合条件的账号。' : '暂无账号运行记录。' }}
    </p>
    <footer class="account-pager">
      <span aria-live="polite"
        >显示 {{ first }}–{{ last }} 项，共 {{ filtered.length }} 个账号
        <small>每页 {{ pageSize }} 项</small></span
      >
      <nav aria-label="账号分页">
        <button type="button" aria-label="账号上一页" :disabled="page <= 1" @click="page--">
          ‹
        </button>
        <button
          v-for="number in pageNumbers"
          :key="number"
          type="button"
          :class="{ active: page === number }"
          :aria-label="'账号第 ' + number + ' 页'"
          :aria-current="page === number ? 'page' : undefined"
          @click="page = number"
        >
          {{ number }}
        </button>
        <button type="button" aria-label="账号下一页" :disabled="page >= pages" @click="page++">
          ›
        </button>
      </nav>
    </footer>
    <div v-if="unassigned.length" class="account-unassigned">
      <h4>尚未关联账号的任务 · {{ unassigned.length }}{{ fresh ? '' : '（上次记录）' }}</h4>
      <div v-for="task in unassigned" :key="task.task_id" class="runtime-detail-task">
        <span class="runtime-stage" :class="fresh ? stageTone(task.stage) : 'gray'">{{
          stageLabel(task.stage)
        }}</span>
        <span class="runtime-detail-task-name"
          >{{ task.title }}
          <small v-if="taskWait(task)" class="runtime-task-wait">
            {{ fresh ? '等待：' : '上次等待：' }}{{ taskWait(task)?.label }}
            <template v-if="taskWait(task)?.detail"> · {{ taskWait(task)?.detail }}</template>
            <template v-if="fresh"> · {{ countdown(taskWait(task)?.until) }}</template>
          </small>
        </span>
        <button
          type="button"
          class="account-text-button"
          :disabled="!fresh"
          :aria-label="'查看 ' + task.title + ' 的日志'"
          @click="openLog(task)"
        >
          日志
        </button>
      </div>
      <small>阶段标签按关联账号的任务计数，不含以上任务。</small>
    </div>
    <TaskLog v-if="log" :key="log.id" :task="log" @close="log = undefined" />
  </section>
</template>

<style scoped>
.account-tasks {
  margin-top: 22px;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: #fff;
  min-width: 0;
}
.account-panel-heading {
  padding: 16px 16px 12px;
  display: flex;
  justify-content: space-between;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
}
.account-panel-title {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 12px;
}
.account-panel-title h3 {
  margin: 0;
  font-size: 15px;
  color: #1e293b;
}
.account-panel-title > span {
  border-left: 1px solid var(--line);
  padding-left: 12px;
  color: #64748b;
  font-size: 11px;
}
.account-sample {
  color: #64748b;
  font-size: 10px;
}
.account-sample::before {
  content: '';
  display: inline-block;
  width: 5px;
  height: 5px;
  background: #16a078;
  border-radius: 50%;
  margin-right: 6px;
}
.account-sample.stale::before {
  background: #94a3b8;
}
.account-toolbar {
  padding: 0 16px 14px;
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
}
.account-toolbar input {
  width: 260px;
  max-width: 100%;
}
.account-toolbar select {
  width: 150px;
}
.account-totals {
  margin-left: auto;
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 14px;
  font-size: 11px;
  color: #64748b;
}
.account-totals > * + * {
  border-left: 1px solid var(--line);
  padding-left: 14px;
}
.account-tasks .account-totals button.cooldown-stat {
  min-height: 22px;
  padding: 1px 0;
  border: 0;
  border-radius: 3px;
  background: transparent;
  color: #64748b;
  font-size: 11px;
}
.account-tasks .account-totals button.cooldown-stat.active,
.account-tasks .account-totals button.cooldown-stat:not(:disabled):hover {
  color: #a66f20;
  background: #fff6e8;
}
.account-tasks .account-totals button.cooldown-stat {
  color: #a66f20;
  padding-left: 14px;
  border-left: 1px solid var(--line);
}
.account-totals b {
  color: #334155;
  font-weight: 600;
}
.account-stage-filter {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 7px;
  padding: 12px 16px;
  background: #f8fafc;
  border-block: 1px solid var(--line);
}
.account-stage-label {
  font-size: 11px;
  color: #64748b;
  margin-right: 4px;
}
.account-tasks button.account-stage-tab {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  min-height: 32px;
  padding: 5px 9px;
  font-size: 11px;
  color: #52657e;
}
.account-stage-count {
  padding: 0 5px;
  min-width: 20px;
  text-align: center;
  border-radius: 4px;
  background: #f1f5f9;
  color: #64748b;
  line-height: 20px;
  font-variant-numeric: tabular-nums;
}
.account-tasks button.account-stage-tab.active {
  background: #eff6ff;
  color: #1d4ed8;
  border-color: #93b4ef;
  font-weight: 600;
  box-shadow: 0 0 0 1px #dbeafe inset;
}
.account-stage-tab.active .account-stage-count {
  background: #2563eb;
  color: #fff;
}
.account-wait-summary {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px;
  padding: 8px 16px;
  border-bottom: 1px solid #edf1f6;
  color: #826225;
  background: #fffdf8;
  font-size: 11px;
}
.account-wait-chip {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 3px 7px;
  border: 1px solid #f0dfba;
  border-radius: 4px;
  background: #fff8e9;
}
.account-wait-chip b {
  font-variant-numeric: tabular-nums;
}
.account-wait-summary > small {
  color: #7d8da2;
  font-size: 10px;
}
.account-filter-note {
  margin: 0;
  padding: 8px 16px;
  background: #fbfdff;
  border-bottom: 1px solid #edf1f6;
  color: #64748b;
  font-size: 11px;
}
.account-filter-note b {
  color: #2563eb;
}
.account-filter-note button {
  margin-left: 6px;
}
.account-columns,
.account-runtime-row {
  display: grid;
  grid-template-columns:
    minmax(0, 1.5fr) minmax(0, 0.9fr) minmax(0, 1.4fr) minmax(85px, 0.7fr)
    minmax(0, 1.8fr) 72px;
  gap: 16px;
  padding: 10px 16px;
  align-items: center;
}
.account-columns {
  font-size: 11px;
  color: #64748b;
  border-bottom: 1px solid var(--line);
}
.account-columns > :last-child {
  text-align: right;
}
.account-runtime-item + .account-runtime-item {
  border-top: 1px solid #edf1f6;
}
.account-runtime-item:nth-child(even) {
  background: #fcfdff;
}
.account-runtime-row {
  min-height: 67px;
}
.account-runtime-row > * {
  min-width: 0;
}
.runtime-identity {
  display: flex;
  align-items: center;
  gap: 9px;
}
.runtime-avatar {
  flex-shrink: 0;
  width: 28px;
  height: 29px;
  display: grid;
  place-items: center;
  border-radius: 7px;
  background: #f0f4fa;
  color: #8495ad;
  font-size: 10px;
}
.runtime-identity-copy {
  min-width: 0;
}
.account-tasks h4 {
  margin: 0;
  color: #334155;
  font-size: 12px;
  font-weight: 600;
}
.runtime-identity h4,
.runtime-task-title {
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.runtime-secondary {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 5px;
  margin-top: 3px;
  color: #7d8da2;
  font-size: 10px;
}
.runtime-secondary:empty {
  display: none;
}
.runtime-state {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  white-space: nowrap;
}
.runtime-state::before {
  content: '';
  width: 4px;
  height: 4px;
  border-radius: 50%;
  background: currentColor;
}
.runtime-state.ready {
  color: #25856a;
}
.runtime-state.recovery,
.runtime-state.cooldown,
.runtime-wait.recovery,
.runtime-wait.cooldown {
  color: #a66f20;
}
.runtime-state.manual,
.runtime-state.unavailable,
.runtime-wait.manual,
.runtime-wait.unavailable {
  color: #c55850;
}
.runtime-stages {
  display: flex;
  gap: 4px;
  flex-wrap: wrap;
  align-items: center;
}
.runtime-stage {
  display: inline-flex;
  align-items: center;
  font-size: 10px;
  border-radius: 4px;
  padding: 3px 6px;
  background: #f1f5f9;
  color: #64748b;
  line-height: 1.5;
  overflow-wrap: anywhere;
}
.runtime-stage.blue {
  background: #eff6ff;
  color: #4275bf;
}
.runtime-stage.purple {
  background: #f3f0fc;
  color: #8067b0;
}
.runtime-stage.amber {
  background: #fff6e8;
  color: #a5772a;
}
.runtime-stage.teal {
  background: #edf8f5;
  color: #398775;
}
.runtime-task-title {
  color: #43536a;
  font-size: 12px;
}
.runtime-speed {
  color: #25856a;
  font-size: 10px;
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}
.runtime-quota {
  font-size: 11px;
  color: #52657e;
  font-variant-numeric: tabular-nums;
}
.runtime-quota b {
  font-size: 14px;
  font-weight: 600;
  color: #334155;
}
.runtime-divider {
  color: #94a3b8;
}
.runtime-quota small,
.runtime-wait small {
  display: block;
  font-size: 10px;
  color: #7d8da2;
  margin-top: 3px;
  overflow-wrap: anywhere;
}
.runtime-meter {
  display: inline-flex;
  gap: 2px;
  vertical-align: middle;
  margin-left: 6px;
}
.runtime-meter i {
  width: 9px;
  height: 4px;
  border-radius: 2px;
  background: #e7edf6;
}
.runtime-meter i.on {
  background: #8eaeeb;
}
.runtime-wait {
  font-size: 11px;
  color: #64748b;
  line-height: 1.55;
}
.runtime-wait .runtime-wait-detail {
  display: block;
  margin-left: 0;
  flex-basis: 100%;
}
.runtime-reason {
  display: inline;
  overflow-wrap: anywhere;
}
.runtime-wait-entry {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: 1px 6px;
}
.runtime-wait-entry + .runtime-wait-entry {
  margin-top: 3px;
}
.runtime-account-waits:not(:first-child) {
  margin-top: 5px;
  padding-top: 4px;
  border-top: 1px dashed #e8edf4;
}
.runtime-wait-count {
  color: #8492a6;
  font-size: 10px;
  white-space: nowrap;
}
.runtime-wait .runtime-countdown {
  display: inline;
  margin: 0;
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}
.runtime-wait .runtime-countdown.timed {
  color: #a66f20;
  font-weight: 600;
}
.runtime-wait .runtime-wait-caption {
  display: block;
  margin: 0 0 3px;
  font-size: 9px;
  color: #8492a6;
}
.runtime-muted {
  color: #8492a6;
}
.runtime-actions {
  display: flex;
  justify-content: flex-end;
  align-items: center;
  gap: 10px;
  white-space: nowrap;
}
.account-tasks button.account-text-button {
  min-height: 26px;
  padding: 2px 0;
  border: 0;
  border-radius: 3px;
  background: transparent;
  color: #2563eb;
  font-size: 11px;
  box-shadow: none;
}
.account-tasks button.account-text-button:hover {
  background: transparent;
  text-decoration: underline;
}
.account-tasks button.account-text-button:disabled {
  color: #94a3b8;
}
.account-tasks button.runtime-details-toggle {
  color: #64748b;
}
.runtime-secondary button.account-text-button,
.runtime-wait button.account-text-button {
  font-size: 10px;
  min-height: 18px;
}
.runtime-details {
  padding: 14px 16px;
  background: #f8fafc;
  border-top: 1px dashed #dbe2ec;
  display: grid;
  grid-template-columns: minmax(0, 1.25fr) minmax(0, 1fr);
  gap: 20px;
}
.runtime-details > section {
  min-width: 0;
}
.runtime-details h4 {
  margin-bottom: 9px;
  font-size: 11px;
}
.runtime-details p {
  font-size: 11px;
  margin: 7px 0;
}
.runtime-detail-task {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  padding: 7px 0;
  border-bottom: 1px solid #e9eef5;
  font-size: 11px;
}
.runtime-detail-task > .runtime-stage {
  flex-shrink: 0;
  max-width: 112px;
}
.runtime-detail-task-name {
  flex: 1;
  min-width: 0;
  overflow-wrap: anywhere;
}
.runtime-detail-task-name small {
  display: block;
  color: #7d8da2;
  font-size: 10px;
  margin-top: 3px;
}
.runtime-detail-task-name .runtime-task-wait {
  color: #926a29;
}
.runtime-detail-task > button {
  flex-shrink: 0;
}
.runtime-reasons {
  padding-left: 16px;
  margin: 0 0 8px;
  font-size: 11px;
  overflow-wrap: anywhere;
}
.runtime-reasons li + li {
  margin-top: 7px;
}
.runtime-reasons small {
  display: block;
  color: #64748b;
  margin-top: 2px;
  font-size: 10px;
}
.runtime-details .text-link {
  font-size: 11px;
}
.account-empty {
  padding: 28px 16px;
  text-align: center;
  color: #64748b;
  margin: 0;
}
.account-pager {
  border-top: 1px solid var(--line);
  padding: 12px 16px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 10px;
  color: #64748b;
  font-size: 11px;
}
.account-pager small {
  margin-left: 8px;
  color: #8492a6;
  font-size: 10px;
}
.account-pager nav {
  display: flex;
  gap: 5px;
}
.account-tasks .account-pager button {
  min-height: 28px;
  min-width: 28px;
  padding: 3px 7px;
  font-size: 11px;
  color: #64748b;
}
.account-tasks .account-pager button.active {
  background: #eff6ff;
  color: #2563eb;
  border-color: #c4d8fd;
}
.account-unassigned {
  padding: 14px 16px;
  border-top: 1px solid var(--line);
}
.account-unassigned > small {
  display: block;
  color: #64748b;
  font-size: 10px;
  margin-top: 8px;
}
.account-mobile-label {
  display: none;
}
@media (max-width: 1150px) {
  .account-columns {
    display: none;
  }
  .account-runtime-row {
    position: relative;
    grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
    gap: 8px 16px;
    padding: 12px 14px;
  }
  .runtime-identity {
    grid-column: 1 / -1;
    padding-right: 82px;
  }
  .runtime-actions {
    position: absolute;
    right: 14px;
    top: 12px;
  }
  .runtime-stages {
    grid-column: 1;
  }
  .runtime-task {
    grid-column: 2;
    grid-row: 2;
  }
  .runtime-quota {
    grid-column: 1;
    display: flex;
    align-items: baseline;
    gap: 8px;
  }
  .runtime-wait {
    grid-column: 2;
    grid-row: 3;
  }
  .account-mobile-label {
    display: inline;
  }
  .account-totals {
    margin-left: 0;
    gap: 8px;
  }
  .runtime-details {
    grid-template-columns: minmax(0, 1fr);
    gap: 16px;
  }
}
@media (max-width: 560px) {
  .account-runtime-row {
    grid-template-columns: minmax(0, auto) minmax(0, 1fr);
  }
  .account-panel-heading {
    padding: 14px 12px 10px;
    align-items: flex-start;
  }
  .account-panel-title {
    gap: 3px;
    display: block;
  }
  .account-panel-title > span {
    display: block;
    border: 0;
    padding: 0;
    font-size: 10px;
    margin-top: 3px;
  }
  .account-totals > * + *,
  .account-tasks .account-totals button.cooldown-stat {
    padding-left: 8px;
  }
  .account-sample {
    max-width: 110px;
    text-align: right;
  }
  .account-toolbar {
    padding: 0 12px 12px;
    gap: 6px;
  }
  .account-toolbar input {
    width: 0;
    flex: 1;
    min-width: 110px;
    font-size: 11px;
  }
  .account-toolbar select {
    width: 125px;
    font-size: 11px;
  }
  .account-totals {
    width: 100%;
    font-size: 10px;
    margin-top: 3px;
  }
  .account-stage-filter {
    padding: 10px 12px;
    gap: 6px;
  }
  .account-stage-label {
    position: absolute;
    width: 1px;
    height: 1px;
    overflow: hidden;
    clip-path: inset(50%);
  }
  .account-tasks button.account-stage-tab {
    min-height: 30px;
    font-size: 10px;
    padding: 4px 7px;
    gap: 5px;
  }
  .account-stage-count {
    min-width: 17px;
    line-height: 18px;
  }
  .account-runtime-row {
    gap: 7px;
    padding: 12px;
  }
  .runtime-identity h4 {
    font-size: 11px;
  }
  .runtime-avatar {
    width: 25px;
    height: 27px;
  }
  .runtime-secondary {
    font-size: 9px;
  }
  .runtime-identity {
    padding-right: 67px;
  }
  .runtime-actions {
    right: 12px;
    gap: 8px;
  }
  .runtime-actions button.account-text-button {
    font-size: 10px;
  }
  .runtime-stages {
    grid-column: 1;
    grid-row: 2;
    max-width: 105px;
  }
  .runtime-task {
    grid-column: 2;
    grid-row: 2;
  }
  .runtime-task-title {
    font-size: 11px;
  }
  .runtime-quota {
    grid-column: 1 / -1;
    grid-row: 3;
  }
  .runtime-wait {
    grid-column: 1 / -1;
    grid-row: 4;
    border-top: 1px dashed #e8edf4;
    padding-top: 6px;
    font-size: 10px;
  }
  .runtime-wait:has(> .runtime-muted:only-child) {
    display: none;
  }
  .runtime-wait small {
    display: inline;
    margin-left: 6px;
  }
  .runtime-details {
    padding: 12px;
  }
  .runtime-detail-task {
    gap: 6px;
    flex-wrap: wrap;
  }
  .runtime-detail-task > .runtime-stage {
    max-width: 90px;
  }
  .account-filter-note {
    padding: 8px 12px;
    font-size: 10px;
  }
  .account-pager {
    padding: 10px 12px;
    font-size: 10px;
  }
  .account-pager small {
    display: none;
  }
}
</style>
