<script setup lang="ts">
import { ref, watch } from 'vue';
import { api } from '../../api';
import WorkspaceDialog from '../../components/WorkspaceDialog.vue';
import { useOperation } from '../../composables/operation';
import { confirmNavigation, useLeaveGuard } from '../../composables/leaveGuard';
import { base, emailCodeOrigin } from './contracts';
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
    class="account-import-dialog"
    @close="emit('close')"
  >
    <div class="import-steps">
      <span class="active"><b>1</b>填写账号</span><i></i
      ><span :class="{ active: !!preview }"><b>2</b>预览并确认</span>
    </div>
    <p v-if="op.error.value" class="notice error" role="alert">{{ op.error.value }}</p>
    <form id="account-import-form" @submit.prevent="inspect">
      <fieldset :disabled="op.busy.value" class="import-fields">
        <label class="import-text-label"
          >账号资料<span>每行一个账号 · 最多 1000 行 / 1 MB</span
          ><textarea
            v-model="text"
            required
            rows="7"
            autocomplete="off"
            spellcheck="false"
            placeholder="邮箱----密码----附加信息（选填）&#10;demo@example.com----password&#10;other@example.com----password----附加信息"
          ></textarea>
        </label>
        <div class="import-options">
          <label
            >或选择 UTF-8 文本文件<input
              type="file"
              accept=".txt,text/plain"
              @change="file" /></label
          ><label
            >重复邮箱处理<select v-model="mode">
              <option value="skip">跳过已有账号</option>
              <option value="update">更新资料，保留绑定与 Cookie</option>
            </select></label
          >
        </div>
      </fieldset>
    </form>
    <p class="import-provider">
      所有账号统一使用
      <a :href="emailCodeOrigin" target="_blank" rel="noopener noreferrer">fbvjdjcf.asia ↗</a>
      接码，无需填写接码网址。
    </p>
    <section v-if="preview" class="import-preview">
      <div class="import-result-heading">
        <strong>导入预览</strong
        ><span
          >共 {{ preview.items.length }} 行 ·
          <span :class="{ error: preview.errors }">错误 {{ preview.errors }} 行</span></span
        >
      </div>
      <div class="table-scroll">
        <table>
          <thead>
            <tr>
              <th>行号</th>
              <th>邮箱账号</th>
              <th>处理结果</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="row in preview.items" :key="row.line">
              <td>{{ row.line }}</td>
              <td>{{ row.email || '—' }}</td>
              <td :class="{ error: !!row.error }">{{ row.error || actionLabels[row.action] }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>
    <footer class="import-footer">
      <small>预览不会保存账号或发起登录</small
      ><button :disabled="op.busy.value" form="account-import-form" :class="{ primary: !preview }">
        {{ preview ? '重新预览' : '预览账号' }}</button
      ><button
        v-if="preview"
        class="primary"
        :disabled="op.busy.value || !!preview.errors"
        @click="commit"
      >
        确认导入 {{ preview.items.length }} 个账号
      </button>
    </footer>
  </WorkspaceDialog>
</template>
<style scoped>
.account-import-dialog {
  width: min(760px, calc(100vw - 32px));
}
.account-import-dialog :deep(.modal-heading h2) {
  font-size: 17px;
}
.import-steps {
  display: flex;
  align-items: center;
  gap: 14px;
  padding-bottom: 20px;
  margin-bottom: 20px;
  border-bottom: 1px solid var(--line);
}
.import-steps > span {
  display: flex;
  align-items: center;
  gap: 8px;
  color: #94a3b8;
  font-size: 12px;
}
.import-steps b {
  display: grid;
  place-items: center;
  width: 22px;
  height: 22px;
  border-radius: 50%;
  background: #f1f5f9;
  font-size: 11px;
}
.import-steps .active {
  color: #168578;
}
.import-steps .active b {
  background: #e9f6f1;
}
.import-steps i {
  width: 30px;
  height: 1px;
  background: var(--line);
}
.import-fields {
  display: grid;
  gap: 16px;
}
.import-fields label {
  display: flex;
  flex-direction: column;
  gap: 7px;
  font-size: 12px;
  color: #475569;
  min-width: 0;
}
.import-text-label > span {
  color: var(--muted);
  font-size: 11px;
}
.import-fields textarea {
  font-family: Consolas, 'Microsoft YaHei', monospace;
  font-size: 12px;
  line-height: 1.8;
  resize: vertical;
  min-height: 170px;
}
.import-options {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 20px;
}
.import-options input {
  font-size: 11px;
}
.import-provider {
  font-size: 11px;
  color: var(--muted);
  margin: 16px 0;
}
.import-provider a {
  color: #168578;
}
.import-preview {
  border: 1px solid var(--line);
  border-radius: 8px;
  overflow: hidden;
}
.import-result-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 12px 14px;
  font-size: 12px;
  background: #f8fafc;
}
.import-result-heading > span {
  font-size: 11px;
  color: var(--muted);
}
.import-preview .table-scroll {
  max-height: 240px;
}
.import-preview table {
  margin: 0;
}
.import-footer {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 22px;
  border-top: 1px solid var(--line);
  padding-top: 18px;
}
.import-footer small {
  margin-right: auto;
  color: var(--muted);
  font-size: 11px;
}
.account-import-dialog .primary {
  background: #168578;
  border-color: #168578;
}
@media (max-width: 600px) {
  .import-options {
    grid-template-columns: 1fr;
    gap: 14px;
  }
  .import-footer small {
    flex-basis: 100%;
  }
}
</style>
