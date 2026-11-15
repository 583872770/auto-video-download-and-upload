<script setup lang="ts">
import { computed, ref } from 'vue';
import { api } from '../../api';
import WorkspaceDialog from '../../components/WorkspaceDialog.vue';
import { useOperation } from '../../composables/operation';
import { usePolling } from '../../composables/polling';
import { useLeaveGuard } from '../../composables/leaveGuard';
import { base, emailCodeOrigin, type Account, type Node } from './contracts';
const props = defineProps<{ item: Account; nodes: Node[] }>(),
  emit = defineEmits<{ close: []; changed: [] }>();
interface Login {
  id?: string;
  state: string;
  message: string;
  active: boolean;
  can_cancel: boolean;
  connection?: string;
  account?: { instance_id: string; version: number };
}
const item = ref(props.item),
  target = ref(props.item.instance_id || ''),
  automatic = ref(true),
  status = ref<Login>(),
  op = useOperation();
useLeaveGuard(() => ({ dirty: false, busy: op.busy.value }));
async function refresh(signal?: AbortSignal) {
  const latest = (await api.request<{ item: Account }>(base + '/' + item.value.id, { signal }))
    .item;
  const value = latest.instance_id
    ? await api.request<Login>(base + '/' + latest.id + '/login', { signal })
    : { state: 'idle', message: '请选择实例后开始登录。', active: false, can_cancel: false };
  if (!signal?.aborted) {
    item.value = latest;
    if (latest.instance_id) target.value = latest.instance_id;
    status.value = value;
  }
}
async function action(cancel: boolean) {
  await op.run(async () => {
    try {
      status.value = await api.request<Login>(
        base + '/' + item.value.id + '/login' + (cancel ? '/cancel' : ''),
        {
          method: 'POST',
          body: cancel
            ? { session_id: status.value?.id }
            : {
                version: item.value.version,
                instance_id: target.value,
                automatic: automatic.value,
              },
        },
      );
      if (status.value.account) {
        item.value = {
          ...item.value,
          instance_id: status.value.account.instance_id,
          version: status.value.account.version,
        };
        target.value = status.value.account.instance_id;
      }
      emit('changed');
    } catch (error) {
      status.value = undefined;
      await refresh().catch(() => {});
      throw error;
    }
  });
}
usePolling(
  computed(() => !op.busy.value),
  async (signal) => {
    try {
      await refresh(signal);
    } catch (error) {
      if (!signal?.aborted) {
        status.value = undefined;
        op.error.value = String(error);
      }
    }
  },
  2000,
);
</script>
<template>
  <WorkspaceDialog
    :open="true"
    title="登录账号"
    :busy="op.busy.value"
    class="account-login-dialog"
    @close="emit('close')"
  >
    <div class="login-account-name">{{ item.email }}</div>
    <p class="login-description">自动填写账号资料，登录成功后保存 Cookie。</p>
    <p v-if="op.error.value" class="notice error" role="alert">{{ op.error.value }}</p>
    <div class="login-options">
      <label
        >登录实例<select
          v-model="target"
          :disabled="op.busy.value || !!item.instance_id || status?.active"
        >
          <option value="">请选择实例</option>
          <option v-for="node in nodes" :key="node.id" :value="node.id" :disabled="!node.online">
            {{ node.name }}{{ node.online ? '' : '（离线）' }}
          </option>
        </select></label
      >
      <label class="login-automatic"
        ><input
          v-model="automatic"
          type="checkbox"
          :disabled="op.busy.value || status?.active"
        /><span
          ><strong>自动填写并接收邮件验证码</strong
          ><small>使用已保存的邮箱、密码和固定接码服务</small></span
        ></label
      >
      <div class="login-provider">
        <span>邮件接码</span
        ><a :href="emailCodeOrigin" target="_blank" rel="noopener noreferrer">fbvjdjcf.asia ↗</a
        ><small>系统固定</small>
      </div>
    </div>
    <div class="login-status" role="status">
      <span class="login-status-dot" :class="{ active: status?.active }"></span>
      <div>
        <strong>{{
          status?.active ? '登录进行中' : status?.state === 'succeeded' ? '登录成功' : '登录状态'
        }}</strong>
        <p>{{ status?.message || '正在核对登录状态…' }}</p>
      </div>
    </div>
    <details class="login-help">
      <summary>验证方式与远程窗口说明</summary>
      <p>邮件每 30 秒检查一次，最多等待 3 分钟。短信、验证器和图片验证需要在浏览器中完成。</p>
      <p>服务器登录窗口通过 noVNC 远程桌面查看。关闭此面板不会取消登录；登录最多等待 10 分钟。</p>
      <p v-if="status?.connection">{{ status.connection }}</p>
    </details>
    <div class="login-footer">
      <button :disabled="op.busy.value" @click="emit('close')">返回账号</button
      ><button v-if="status?.can_cancel" :disabled="op.busy.value" @click="action(true)">
        取消登录</button
      ><button
        class="primary"
        :disabled="op.busy.value || !target || !status || status.active"
        @click="action(false)"
      >
        {{ status?.active ? '正在登录…' : '开始登录' }}
      </button>
    </div>
  </WorkspaceDialog>
</template>
<style scoped>
.account-login-dialog {
  width: min(540px, calc(100vw - 32px));
}
.account-login-dialog :deep(.modal-heading h2) {
  font-size: 17px;
}
.login-account-name {
  font-size: 17px;
  font-weight: 600;
  overflow-wrap: anywhere;
}
.login-description {
  color: var(--muted);
  margin: 5px 0 22px;
  font-size: 12px;
}
.login-options {
  display: grid;
  gap: 18px;
}
.login-options > label:first-child {
  display: grid;
  gap: 7px;
  font-size: 12px;
  color: #475569;
}
.login-automatic {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  padding: 14px;
  border: 1px solid #d4e8e2;
  border-radius: 8px;
  background: #f5faf8;
}
.login-automatic strong {
  display: block;
  font-size: 13px;
  font-weight: 500;
}
.login-automatic small {
  display: block;
  font-size: 11px;
  color: var(--muted);
  margin-top: 3px;
}
.login-provider {
  display: flex;
  align-items: center;
  gap: 14px;
  font-size: 12px;
}
.login-provider > span,
.login-provider > small {
  color: var(--muted);
}
.login-provider a {
  color: #168578;
  text-decoration: none;
}
.login-provider small {
  font-size: 10px;
  margin-left: auto;
}
.login-status {
  display: flex;
  gap: 10px;
  margin: 22px 0 16px;
  padding: 16px;
  background: #f8fafc;
  border: 1px solid var(--line);
  border-radius: 8px;
}
.login-status-dot {
  width: 7px;
  height: 7px;
  background: #94a3b8;
  border-radius: 50%;
  flex-shrink: 0;
  margin-top: 7px;
}
.login-status-dot.active {
  background: #168578;
  box-shadow: 0 0 0 3px #16857815;
}
.login-status strong {
  font-size: 12px;
}
.login-status p {
  margin: 5px 0 0;
  font-size: 12px;
  color: #64748b;
  overflow-wrap: anywhere;
}
.login-help {
  font-size: 11px;
  color: var(--muted);
}
.login-help summary {
  cursor: pointer;
}
.login-help p {
  margin: 8px 0;
}
.login-footer {
  display: flex;
  justify-content: flex-end;
  flex-wrap: wrap;
  gap: 8px;
  border-top: 1px solid var(--line);
  padding-top: 18px;
  margin-top: 22px;
}
.login-footer > button:first-child {
  margin-right: auto;
}
.account-login-dialog .primary {
  background: #168578;
  border-color: #168578;
}
</style>
