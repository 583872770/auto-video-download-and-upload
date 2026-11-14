<script setup lang="ts">
import { computed, ref } from 'vue';
import { RouterLink } from 'vue-router';
import { api } from '../../api';
import { useResource } from '../../composables/resource';
import { usePolling } from '../../composables/polling';
import {
  gb,
  integer,
  time,
  type Totals,
  type Session,
  type Method,
  type Ledger,
} from './contracts';
import MethodTable from './MethodTable.vue';
import SessionDetail from './SessionDetail.vue';
interface Overview {
  summary: Totals;
  billing: { file_bytes_exact: string; amount: string; unset: number };
  items: Session[];
  active: Session | null;
  methods: Method[];
  next_cursor: string | null;
  tracking_since: string;
  test_tracking_since: string;
  method_tracking_since: string;
}
const query = ref(''),
  applied = ref(''),
  tab = ref('ledger'),
  selected = ref('');
const sessionLabels: Record<string, string> = {
  active: '进行中',
  stopping: '正在暂停',
  paused: '已暂停',
  completed: '已结束',
};
const overview = useResource<Overview>(),
  ledger = useResource<{ items: Ledger[]; next_cursor: string | null }>();
const cursors = ref({ ledger: '', sessions: '' }),
  histories = ref<{ ledger: string[]; sessions: string[] }>({ ledger: [], sessions: [] });
const key = computed(() => (tab.value === 'ledger' ? 'ledger' : 'sessions'));
const exportHref = computed(
  () =>
    '/api/statistics/' +
    (tab.value === 'ledger'
      ? 'lists/export'
      : tab.value === 'methods'
        ? 'methods/export'
        : 'export') +
    '?' +
    new URLSearchParams({ q: applied.value }),
);
async function refresh(signal?: AbortSignal) {
  await Promise.all([
    overview.load(
      (s) =>
        api.request(
          '/api/statistics?' +
            new URLSearchParams({ q: applied.value, cursor: cursors.value.sessions }),
          { signal: s },
        ),
      signal,
    ),
    ledger.load(
      (s) =>
        api.request(
          '/api/statistics/lists?' +
            new URLSearchParams({ q: applied.value, cursor: cursors.value.ledger }),
          { signal: s },
        ),
      signal,
    ),
  ]);
}
function search() {
  applied.value = query.value;
  cursors.value = { ledger: '', sessions: '' };
  histories.value = { ledger: [], sessions: [] };
  void refresh();
}
function page(next: boolean) {
  const k = key.value;
  if (next) {
    histories.value[k].push(cursors.value[k]);
    cursors.value[k] =
      (k === 'ledger' ? ledger.data.value : overview.data.value)?.next_cursor || '';
  } else cursors.value[k] = histories.value[k].pop() || '';
  void refresh();
}
usePolling(
  computed(() => !selected.value),
  refresh,
  10000,
);
</script>
<template>
  <main class="content">
    <div class="page-heading">
      <div>
        <span class="eyebrow">DELIVERY & BILLING</span>
        <h1>交付与账单</h1>
        <p>全局账本。金额按客户任务及单价计算，测试下载不计费。</p>
      </div>
      <button @click="refresh()">刷新</button>
    </div>
    <p v-if="overview.error.value || ledger.error.value" class="notice error" role="alert">
      {{ overview.error.value || ledger.error.value }}
    </p>
    <section v-if="overview.data.value" class="card">
      <div class="metrics">
        <div>
          累计交付<strong>{{ gb(overview.data.value.billing.file_bytes_exact) }}</strong
          ><small>{{ integer(overview.data.value.billing.file_bytes_exact) }} 字节</small>
        </div>
        <div>
          预计费用合计<strong>¥ {{ overview.data.value.billing.amount }}</strong
          ><small>单价待设置 {{ overview.data.value.billing.unset }} 单</small>
        </div>
        <div>
          执行记录交付<strong>{{ integer(overview.data.value.summary.file_count) }} 个视频</strong
          ><small>测试 {{ gb(overview.data.value.summary.test_file_bytes_exact) }} · 不计费</small>
        </div>
      </div>
      <p class="muted">
        统计起始于 {{ time(overview.data.value.tracking_since) }}；测试统计起始于
        {{ time(overview.data.value.test_tracking_since) }}。历史未跟踪记录不补算费用。
      </p>
      <button v-if="overview.data.value.active" @click="selected = overview.data.value!.active!.id">
        查看进行中的会话
      </button>
    </section>
    <section class="card">
      <form class="toolbar" @submit.prevent="search">
        <input v-model="query" aria-label="搜索账单" placeholder="客户任务 / 会话备注" /><button>
          查询</button
        ><select v-model="tab" aria-label="账本范围">
          <option value="ledger">客户任务账本</option>
          <option value="sessions">执行会话</option>
          <option value="methods">下载提供方</option></select
        ><a :href="exportHref" download>导出 CSV</a>
      </form>
      <template v-if="tab === 'ledger'"
        ><article v-for="row in ledger.data.value?.items" :key="row.id" class="ledger-row">
          <h2>
            {{ row.name }}
            <small class="muted"
              >{{ row.customer || '历史未分类' }} ·
              {{ row.deleted ? '已删除，保留账本' : row.archived ? '已归档' : '客户任务' }}</small
            >
          </h2>
          <p>
            累计交付 {{ row.tracked ? gb(row.file_bytes_exact) : '历史未补算' }} ·
            {{ integer(row.file_count) }} 个文件 · {{ integer(row.file_bytes_exact) }} 字节
          </p>
          <p>
            成功 {{ integer(row.succeeded) }} · 失败 {{ integer(row.failed) }} · 测试
            {{ gb(row.test_file_bytes_exact) }}（不计费）
          </p>
          <p>
            预计费用
            {{ !row.tracked ? '—' : row.amount === null ? '待设置单价' : '¥ ' + row.amount }} ·
            {{ row.price_set ? row.price + ' 元 / ' + row.unit : '单价待定' }}
          </p>
          <div class="actions">
            <RouterLink v-if="row.tracked && !row.deleted" :to="'/jobs/' + row.id"
              >任务详情 / 单价</RouterLink
            ><a :href="`/api/statistics/lists/${row.id}/files/export`" download>导出交付 CSV</a
            ><a :href="`/api/statistics/lists/${row.id}/files/export?kind=test`" download
              >导出测试 CSV</a
            >
          </div>
        </article>
        <p v-if="ledger.data.value && !ledger.data.value.items.length" class="empty">
          暂无客户任务账本。
        </p></template
      >
      <div v-else-if="tab === 'sessions'" class="table-scroll">
        <table>
          <thead>
            <tr>
              <th>会话 / 时间</th>
              <th>状态</th>
              <th>成功 / 失败 / 跳过</th>
              <th>交付</th>
              <th>测试（不计费）</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="row in overview.data.value?.items" :key="row.id">
              <td>
                <button @click="selected = row.id">{{ row.label || '下载会话 #' + row.seq }}</button
                ><small class="cell-note"
                  >{{ time(row.started_at) }} 至
                  {{ row.ended_at ? time(row.ended_at) : '当前' }}</small
                >
              </td>
              <td>{{ sessionLabels[row.status] || row.status }}</td>
              <td>
                {{ integer(row.succeeded) }} / {{ integer(row.failed) }} /
                {{ integer(row.skipped) }}
              </td>
              <td>
                {{ gb(row.file_bytes_exact)
                }}<small class="cell-note">{{ integer(row.file_bytes_exact) }} 字节</small>
              </td>
              <td>{{ gb(row.test_file_bytes_exact) }}</td>
            </tr>
          </tbody>
        </table>
      </div>
      <template v-else
        ><MethodTable :items="overview.data.value?.methods || []" />
        <p class="muted">
          提供方统计起始于
          {{
            time(overview.data.value?.method_tracking_since)
          }}。按实际处理视频的提供方归类；成功率包含测试成功，历史未分类记录单列。
        </p></template
      >
      <div v-if="tab !== 'methods'" class="pager">
        <button
          :disabled="!histories[key].length || overview.loading.value || ledger.loading.value"
          @click="page(false)"
        >
          上一页</button
        ><span>第 {{ histories[key].length + 1 }} 页</span
        ><button
          :disabled="
            !(key === 'ledger' ? ledger.data.value : overview.data.value)?.next_cursor ||
            overview.loading.value ||
            ledger.loading.value
          "
          @click="page(true)"
        >
          下一页
        </button>
      </div>
    </section>
    <SessionDetail
      v-if="selected"
      :key="selected"
      :id="selected"
      @close="selected = ''"
      @saved="refresh()"
    />
  </main>
</template>
