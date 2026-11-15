<script setup lang="ts">
import { computed, ref } from 'vue';
import { api } from '../../api';
import { ApiError } from '../../api/client';
import WorkspaceDialog from '../../components/WorkspaceDialog.vue';
import { useOperation } from '../../composables/operation';
import { usePolling } from '../../composables/polling';
import {
  base,
  emailCodeOrigin,
  type AccountSelection,
  type AccountBatchStatus,
  type Node,
} from './contracts';

const props = defineProps<{
  mode: 'login' | 'bind' | 'check';
  items: AccountSelection[];
  nodes: Node[];
  resume?: boolean;
}>();
const emit = defineEmits<{ close: []; changed: [] }>();
const selected = [...props.items];
const checking = props.mode === 'check';
const title = props.mode === 'bind' ? '批量修改实例' : checking ? '批量检测登录' : '批量自动登录';
const endpoint = base + (checking ? '/batch-check' : '/batch-login');
const target = ref(''),
  status = ref<AccountBatchStatus>(),
  checked = ref(props.mode === 'bind'),
  op = useOperation();
const binding = ref<{
  items: Array<{ id: string; state: string; message: string }>;
  succeeded: number;
  failed: number;
}>();
const labels: Record<string, string> = {
  pending: '待处理',
  queued: '排队中',
  waiting: '等待实例',
  running: checking ? '检测中' : '登录中',
  succeeded: '成功',
  failed: '失败',
  needs_attention: '需人工处理',
  skipped: '已跳过',
  cancelled: '已取消',
  interrupted: '已中断',
};
const unbound = selected.filter((row) => !row.instance_id).length;
const rows = computed(
  () =>
    status.value?.items ||
    binding.value?.items.map((result) => ({
      ...selected.find((item) => item.id === result.id)!,
      ...result,
      instance_id:
        result.state === 'succeeded'
          ? target.value
          : selected.find((item) => item.id === result.id)?.instance_id,
    })) ||
    selected.map((row) => ({
      ...row,
      state: 'pending',
      message:
        props.mode === 'bind'
          ? '等待修改所属实例。'
          : checking
            ? '等待检测登录。'
            : '等待自动登录。',
    })),
);
const canStart = computed(
  () =>
    checked.value &&
    selected.length > 0 &&
    !op.busy.value &&
    !status.value &&
    !binding.value &&
    (props.mode === 'bind' ? !!target.value : checking || !unbound || !!target.value),
);
let notified = '';
async function refresh(signal?: AbortSignal, discover = false) {
  const current = await api.request<AccountBatchStatus>(endpoint, { signal });
  if (signal?.aborted) return;
  checked.value = true;
  if (current.active || props.resume || discover || current.id === status.value?.id)
    status.value = current.id ? current : undefined;
  if (status.value?.id && !status.value.active && notified !== status.value.id) {
    notified = status.value.id;
    emit('changed');
  }
}
usePolling(
  computed(
    () => props.mode !== 'bind' && !op.busy.value && (!checked.value || !!status.value?.active),
  ),
  async (signal) => {
    try {
      await refresh(signal);
    } catch (error) {
      if (!signal?.aborted) op.error.value = error instanceof Error ? error.message : String(error);
    }
  },
  2000,
);
async function start() {
  await op.run(async () => {
    const body = {
      items: selected.map(({ id, version }) => ({ id, version })),
      instance_id: target.value || undefined,
    };
    if (props.mode === 'bind') {
      binding.value = await api.request(base + '/batch-bind', { method: 'POST', body });
      emit('changed');
    } else {
      try {
        status.value = await api.request(endpoint, { method: 'POST', body });
        emit('changed');
      } catch (error) {
        if (error instanceof ApiError && (error.status === 0 || error.status === 409))
          await refresh(undefined, true).catch(() => {});
        throw error;
      }
    }
  });
}
async function stop() {
  await op.run(async () => {
    status.value = await api.request(endpoint + '/cancel', {
      method: 'POST',
      body: { id: status.value?.id },
    });
  });
}
const nodeName = (identity?: string | null) =>
  props.nodes.find((node) => node.id === identity)?.name || identity || '尚未绑定';
</script>
<template>
  <WorkspaceDialog
    :open="true"
    :title="title"
    :busy="op.busy.value"
    class="account-batch-dialog"
    @close="emit('close')"
  >
    <div class="batch-intro">
      <strong>{{
        status
          ? `共 ${status.total} 个账号 · 已处理 ${status.finished} 个`
          : `已选择 ${selected.length} 个账号`
      }}</strong>
      <p>
        {{
          mode !== 'bind'
            ? checking
              ? '依次在账号所属实例检测，关闭窗口后仍在后台继续。'
              : '按顺序自动填写、接收邮件验证码并保存 Cookie。关闭此窗口后仍在后台继续。'
            : '将所选账号设置为同一个实例，并逐项展示修改结果。'
        }}
      </p>
    </div>
    <p v-if="op.error.value" class="notice error" role="alert">{{ op.error.value }}</p>
    <form v-if="!status && !binding" class="batch-settings" @submit.prevent="start">
      <label v-if="mode === 'bind' || (mode === 'login' && unbound)"
        >{{ mode === 'bind' ? '目标实例' : `${unbound} 个未绑定账号的登录实例`
        }}<select v-model="target" :disabled="op.busy.value">
          <option value="">请选择实例</option>
          <option
            v-for="node in nodes"
            :key="node.id"
            :value="node.id"
            :disabled="mode === 'login' && !node.online"
          >
            {{ node.name }}{{ node.online ? '' : '（离线）' }}
          </option>
        </select></label
      >
      <p v-if="mode === 'login'" class="batch-help">
        已绑定账号在各自所属实例登录。接码统一使用
        <a :href="emailCodeOrigin" target="_blank" rel="noopener noreferrer">fbvjdjcf.asia ↗</a
        >。需要短信、图片验证或接码不可用时记录原因，继续下一个账号。
      </p>
      <p v-else-if="checking" class="batch-help">
        没有登录凭据或正在冷却的账号会记录原因并跳过。检测通过可恢复人工暂停，现有限流冷却不会清除。网络问题会显示为暂时无法确认，不会直接判定未登录。
      </p>
      <p v-else class="batch-help">
        已有 Cookie
        或保留实例归属的账号无法跨实例改绑；资料版本发生变化时也会跳过，其他账号继续处理。
      </p>
    </form>
    <div v-if="status || binding" class="batch-summary" role="status">
      <span class="batch-success"
        >成功 {{ status?.counts.succeeded || binding?.succeeded || 0 }}</span
      ><span class="batch-failed">失败 {{ status?.counts.failed || binding?.failed || 0 }}</span
      ><span v-if="status">需人工处理 {{ status.counts.needs_attention || 0 }}</span
      ><span v-if="status && checking">已跳过 {{ status.counts.skipped || 0 }}</span
      ><span v-if="status"
        >取消 / 中断 {{ (status.counts.cancelled || 0) + (status.counts.interrupted || 0) }}</span
      >
    </div>
    <p v-if="status" class="batch-message">{{ status.message }}</p>
    <progress
      v-if="status"
      :value="status.finished"
      :max="status.total || 1"
      aria-label="批量操作完成进度"
    ></progress>
    <div class="table-scroll batch-results">
      <table>
        <thead>
          <tr>
            <th>邮箱账号</th>
            <th>所属实例</th>
            <th>结果</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in rows" :key="row.id">
            <td>{{ row.email }}</td>
            <td>{{ nodeName(row.instance_id) }}</td>
            <td>
              <span class="batch-state" :class="row.state">{{
                labels[row.state] || row.state
              }}</span
              ><small>{{ row.message }}</small>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
    <footer class="batch-footer">
      <span>{{
        status?.active
          ? '关闭窗口后仍在后台继续'
          : binding || status
            ? '可以关闭窗口后重新选择账号操作'
            : '操作仅针对本次勾选的账号'
      }}</span>
      <div class="actions">
        <button :disabled="op.busy.value" @click="emit('close')">
          {{ status?.active ? '后台继续' : '关闭' }}</button
        ><button
          v-if="status?.active"
          class="danger"
          :disabled="op.busy.value || status.state === 'stopping'"
          @click="stop"
        >
          {{
            status.state === 'stopping' ? '正在停止…' : checking ? '停止批量检测' : '停止批量登录'
          }}</button
        ><button
          v-else-if="!status && !binding"
          class="primary"
          :disabled="!canStart"
          @click="start"
        >
          {{
            op.busy.value
              ? '处理中…'
              : checking
                ? '开始检测登录'
                : mode === 'login'
                  ? '开始自动登录'
                  : '确认修改实例'
          }}</button
        ><button
          v-if="op.error.value && mode !== 'bind'"
          :disabled="op.busy.value"
          @click="op.run(() => refresh())"
        >
          刷新进度
        </button>
      </div>
    </footer>
  </WorkspaceDialog>
</template>
<style scoped>
.account-batch-dialog {
  width: min(940px, calc(100vw - 32px));
}
.account-batch-dialog :deep(.modal-heading h2) {
  font-size: 17px;
}
.batch-intro strong {
  font-size: 15px;
}
.batch-intro p {
  color: var(--muted);
  font-size: 12px;
  margin: 6px 0 18px;
}
.batch-settings {
  padding: 16px;
  background: #f8fafc;
  border: 1px solid var(--line);
  border-radius: 8px;
  margin-bottom: 18px;
}
.batch-settings label {
  display: flex;
  flex-direction: column;
  gap: 8px;
  max-width: 350px;
  font-size: 12px;
}
.batch-help {
  font-size: 12px;
  color: var(--muted);
  line-height: 1.8;
  margin: 10px 0 0;
}
.batch-help a {
  color: #168578;
}
.batch-summary {
  display: flex;
  flex-wrap: wrap;
  gap: 16px;
  padding: 12px 14px;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: #f8fafc;
  font-size: 12px;
}
.batch-success {
  color: #168578;
}
.batch-failed {
  color: #be5151;
}
.batch-message {
  font-size: 12px;
  color: var(--muted);
}
.account-batch-dialog progress {
  display: block;
  width: 100%;
  max-width: 100%;
  height: 5px;
  accent-color: #168578;
  margin: 14px 0;
}
.batch-results {
  max-height: 400px;
  border: 1px solid var(--line);
  border-radius: 8px;
  margin-top: 16px;
}
.batch-results table {
  margin: 0;
  width: 100%;
}
.batch-results th {
  position: sticky;
  top: 0;
  background: #f8fafc;
  z-index: 1;
}
.batch-results th:nth-child(1) {
  width: 32%;
}
.batch-results th:nth-child(2) {
  width: 20%;
}
.batch-results td {
  vertical-align: top;
  overflow-wrap: anywhere;
}
.batch-results small {
  display: block;
  color: var(--muted);
  font-size: 11px;
  margin-top: 5px;
}
.batch-state {
  font-size: 11px;
  color: #64748b;
}
.batch-state.succeeded {
  color: #168578;
}
.batch-state.failed {
  color: #be5151;
}
.batch-state.needs_attention {
  color: #a26f22;
}
.batch-state.running,
.batch-state.waiting {
  color: #2563eb;
}
.batch-footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 12px;
  margin-top: 20px;
  padding-top: 18px;
  border-top: 1px solid var(--line);
}
.batch-footer > span {
  font-size: 11px;
  color: var(--muted);
}
.account-batch-dialog .primary {
  background: #168578;
  border-color: #168578;
}
@media (max-width: 600px) {
  .batch-results table {
    min-width: 600px;
  }
  .batch-footer > .actions {
    margin-left: auto;
  }
}
</style>
