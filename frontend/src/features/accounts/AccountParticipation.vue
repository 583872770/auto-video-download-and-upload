<script setup lang="ts">
import { computed, ref } from 'vue';
import { api } from '../../api';
import WorkspaceDialog from '../../components/WorkspaceDialog.vue';
import { useOperation } from '../../composables/operation';
import { useLeaveGuard } from '../../composables/leaveGuard';
import { base, type Account, type Node } from './contracts';
import type { ConsoleConfig } from '../../generated/config';
const props = defineProps<{ items: Account[]; nodes: Node[]; mode: 'join' | 'remove' }>();
const emit = defineEmits<{ close: []; changed: [] }>();
const selected = [...props.items],
  op = useOperation();
useLeaveGuard(() => ({ dirty: false, busy: op.busy.value }));
const results = ref<Array<{ id: string; name: string; message: string; ok: boolean }>>([]);
const finished = ref(false);
const groups = computed(() => [...new Set(selected.map((row) => row.instance_id).filter(Boolean))]);
async function save() {
  await op.run(async () => {
    results.value = [];
    const { items } = await api.request<{
      items: Array<Pick<Account, 'id' | 'version' | 'instance_id'>>;
    }>(base + '/choices');
    const currentAccounts = new Map(items.map((item) => [item.id, item]));
    for (const target of groups.value) {
      const name = props.nodes.find((node) => node.id === target)?.name || target!;
      try {
        const scope = { id: target!, epoch: 0, isCurrent: () => true };
        const rows = selected.filter((row) => row.instance_id === target);
        // One metadata read fences all selections without one request per account.
        const profiles: string[] = [];
        for (const row of rows) {
          const item = currentAccounts.get(row.id);
          if (!item || item.version !== row.version || item.instance_id !== target)
            throw new Error('账号资料已变化，请刷新清单后重新选择。');
          if (props.mode === 'join' && !row.cookies.length)
            throw new Error('所选账号中有尚未登录的账号，请先登录后再加入下载。');
          profiles.push(...row.cookies.map((cookie) => cookie.id));
        }
        const { config } = await api.request<{ config: ConsoleConfig }>('/api/config', { scope });
        const before = config.youtube_cookie_pool;
        const ids =
          props.mode === 'join'
            ? [...new Set([...before, ...profiles])]
            : before.filter((id) => !profiles.includes(id));
        if (ids.length === 1)
          throw new Error(
            '账号池至少需要 2 个账号，请同时选择至少 2 个加入，或将剩余账号全部移出。单账号可在详情中选择“单独使用”。',
          );
        const body: Record<string, unknown> = { youtube_cookie_pool: ids };
        const current = config.youtube_cookie
          .replaceAll('\\', '/')
          .split('/')
          .at(-1)
          ?.replace(/\.txt$/, '');
        if (props.mode === 'remove' && profiles.includes(current || '')) body.youtube_cookie = '';
        await api.request('/api/config', { method: 'PUT', scope, body });
        const { config: saved } = await api.request<{ config: ConsoleConfig }>('/api/config', {
          scope,
        });
        if (
          JSON.stringify(saved.youtube_cookie_pool) !== JSON.stringify(ids) ||
          (body.youtube_cookie === '' && saved.youtube_cookie !== '')
        )
          throw new Error('保存结果尚未确认，请刷新清单核对。');
        results.value.push({
          id: target!,
          name,
          ok: true,
          message: `已${props.mode === 'join' ? '加入' : '移出'}；该实例现有 ${ids.length} 个池内账号。`,
        });
      } catch (error) {
        results.value.push({
          id: target!,
          name,
          ok: false,
          message: error instanceof Error ? error.message : String(error),
        });
      }
    }
    for (const row of selected.filter((item) => !item.instance_id))
      results.value.push({
        id: row.id,
        name: row.email,
        ok: false,
        message: '尚未分配实例，请先分配并登录。',
      });
    finished.value = true;
    emit('changed');
  });
}
</script>
<template>
  <WorkspaceDialog
    :open="true"
    :title="mode === 'join' ? '加入下载' : '移出下载'"
    :busy="op.busy.value"
    @close="emit('close')"
  >
    <p>已选择 {{ selected.length }} 个账号，涉及 {{ groups.length }} 个实例。</p>
    <p class="muted">
      {{
        mode === 'join'
          ? '将账号加入各自所属实例的下载账号池，保留其他成员。账号需先完成登录，每个账号池至少需要 2 个账号。'
          : '从各自所属实例移出所选账号，不删除账号资料或登录凭据。'
      }}账号冷却与运行记录均保留。
    </p>
    <p v-if="op.error.value" class="notice error" role="alert">{{ op.error.value }}</p>
    <p v-if="op.busy.value" role="status">正在逐实例保存，请等待结果…</p>
    <article
      v-for="result in results"
      :key="result.id"
      class="notice"
      :class="{ error: !result.ok }"
    >
      <strong>{{ result.name }}</strong>
      <p>{{ result.message }}</p>
    </article>
    <div class="actions">
      <button :disabled="op.busy.value" @click="emit('close')">关闭</button
      ><button v-if="!finished" class="primary" :disabled="op.busy.value" @click="save">
        确认{{ mode === 'join' ? '加入' : '移出' }}
      </button>
    </div>
  </WorkspaceDialog>
</template>
