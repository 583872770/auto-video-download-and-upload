<script setup lang="ts">
import { ref, watch } from 'vue';
import { api } from '../../api';
import WorkspaceDialog from '../../components/WorkspaceDialog.vue';
import { useOperation } from '../../composables/operation';
import { confirmNavigation, useLeaveGuard } from '../../composables/leaveGuard';
import { base } from './contracts';
const emit = defineEmits<{ close: []; saved: [] }>(),
  op = useOperation();
const actionLabels: Record<string, string> = { create: '新增', update: '更新资料', skip: '跳过' };
const text = ref(''),
  mode = ref('skip'),
  preview = ref<{
    token: string;
    items: Array<{ line: number; email?: string; action: string; error?: string }>;
    errors: number;
  }>();
watch([text, mode], () => (preview.value = undefined), { flush: 'sync' });
useLeaveGuard(() => ({ dirty: !!text.value, busy: op.busy.value }));
async function file(event: Event) {
  await op.run(async () => {
    const f = (event.target as HTMLInputElement).files?.[0];
    if (!f) return;
    if (f.size > 1024 * 1024) throw new Error('文本最大 1 MB。');
    text.value = new TextDecoder('utf-8', { fatal: true }).decode(await f.arrayBuffer());
  });
}
async function inspect() {
  await op.run(async () => {
    preview.value = await api.request(base + '/import/preview', {
      method: 'POST',
      body: { text: text.value, mode: mode.value },
    });
  });
}
async function commit() {
  await op.run(async () => {
    if (!preview.value) return;
    await api.request(base + '/import', {
      method: 'POST',
      body: { text: text.value, token: preview.value.token },
    });
    text.value = '';
    emit('saved');
    emit('close');
  });
}
</script>
<template>
  <WorkspaceDialog
    :open="true"
    title="批量导入账号"
    :busy="op.busy.value"
    :before-close="confirmNavigation"
    @close="emit('close')"
    ><p>
      每行：邮箱----密码----附加信息----接码网址。可只填前两段，最多 1000 行、1
      MB。预览不会保存或登录。
    </p>
    <p v-if="op.error.value" class="notice error" role="alert">{{ op.error.value }}</p>
    <form @submit.prevent="inspect">
      <fieldset :disabled="op.busy.value">
        <label
          >导入文本<textarea
            v-model="text"
            required
            rows="8"
            autocomplete="off"
            spellcheck="false"
          ></textarea></label
        ><label>UTF-8 TXT 文件<input type="file" accept=".txt,text/plain" @change="file" /></label
        ><label
          >重复邮箱处理<select v-model="mode">
            <option value="skip">跳过（推荐）</option>
            <option value="update">更新本次提供资料，保留绑定与 Cookie</option>
          </select></label
        ><button>预览</button>
      </fieldset>
    </form>
    <template v-if="preview"
      ><p>共 {{ preview.items.length }} 行 · 错误 {{ preview.errors }} 行</p>
      <div class="table-scroll">
        <table>
          <thead>
            <tr>
              <th>行号</th>
              <th>邮箱</th>
              <th>结果</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="row in preview.items" :key="row.line">
              <td>{{ row.line }}</td>
              <td>{{ row.email || '—' }}</td>
              <td>{{ row.error || actionLabels[row.action] }}</td>
            </tr>
          </tbody>
        </table>
      </div>
      <button :disabled="op.busy.value || !!preview.errors" @click="commit">
        确认导入
      </button></template
    ></WorkspaceDialog
  >
</template>
