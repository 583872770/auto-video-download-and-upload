<script setup lang="ts">
import { computed, ref } from 'vue';
import { api } from '../../api';
import WorkspaceDialog from '../../components/WorkspaceDialog.vue';
import { useOperation } from '../../composables/operation';
import { usePolling } from '../../composables/polling';
import { useLeaveGuard } from '../../composables/leaveGuard';
import { base, type Account, type Node } from './contracts';
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
    :title="'登录：' + item.email"
    :busy="op.busy.value"
    @close="emit('close')"
    ><p>
      浏览器在账号绑定实例打开；成功核对邮箱后替换旧
      Cookie，保留账号池选择和冷却。关闭此面板不会取消登录。
    </p>
    <p v-if="op.error.value" class="notice error" role="alert">{{ op.error.value }}</p>
    <label
      >绑定实例<select
        v-model="target"
        :disabled="op.busy.value || !!item.instance_id || status?.active"
      >
        <option value="">请选择实例</option>
        <option v-for="node in nodes" :key="node.id" :value="node.id" :disabled="!node.online">
          {{ node.name }} · {{ node.id }}{{ node.online ? '' : '（离线）' }}
        </option>
      </select></label
    ><label
      ><input
        v-model="automatic"
        type="checkbox"
        :disabled="op.busy.value || status?.active"
      />自动填写邮箱、密码并接收邮件验证码</label
    >
    <p class="muted">
      邮件接码使用资料中的 fbvjdjcf.asia 网址，每 30 秒检查一次，最多等待 3
      分钟。短信、验证器或图片验证请在浏览器完成。Docker 实例通过该服务器 noVNC
      远程桌面查看窗口；登录最多等待 10 分钟。
    </p>
    <p role="status">{{ status?.message || '正在核对登录状态…' }}</p>
    <p>{{ status?.connection }}</p>
    <div class="actions">
      <button
        :disabled="op.busy.value || !target || !status || status.active"
        @click="action(false)"
      >
        开始登录</button
      ><button v-if="status?.can_cancel" :disabled="op.busy.value" @click="action(true)">
        取消此会话
      </button>
    </div></WorkspaceDialog
  >
</template>
