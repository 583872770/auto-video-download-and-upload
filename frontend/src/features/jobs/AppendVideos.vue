<script setup lang="ts">
import { ref } from 'vue';
import { api } from '../../api';
import { confirmNavigation, useLeaveGuard } from '../../composables/leaveGuard';
import WorkspaceDialog from '../../components/WorkspaceDialog.vue';
import { jobEndpoint, type Job } from './contracts';
const props = defineProps<{ job: Job }>();
const emit = defineEmits<{ close: []; completed: [message: string] }>();
const urls = ref(''),
  file = ref<File>(),
  busy = ref(false),
  error = ref(''),
  summary = ref('');
useLeaveGuard(() => ({ dirty: !!urls.value.trim() || !!file.value, busy: busy.value }));
async function submit() {
  if (busy.value) return;
  error.value = '';
  if (!urls.value.trim() && !file.value) {
    error.value = '请粘贴视频 ID 或选择导入文件。';
    return;
  }
  if (file.value && (!/\.(txt|csv)$/i.test(file.value.name) || file.value.size > 1024 ** 3)) {
    error.value = '请选择不超过 1 GB 的 UTF-8 TXT / CSV 文件。';
    return;
  }
  busy.value = true;
  try {
    if (urls.value.trim()) {
      const result = await api.request<{ added: number; restored: number; duplicates: number }>(
        jobEndpoint(props.job.id) + '/videos',
        { method: 'POST', body: { urls: urls.value } },
      );
      summary.value = `新增 ${result.added}，恢复 ${result.restored}，重复 ${result.duplicates}。`;
      urls.value = ''; // A failed upload must not resubmit the successful append on retry.
    }
    if (file.value) {
      const body = new FormData();
      body.set('file', file.value);
      await api.request(jobEndpoint(props.job.id) + '/imports', { method: 'POST', body });
      file.value = undefined;
    }
    busy.value = false;
    emit('completed', summary.value || '导入已提交到当前任务。');
  } catch (failure) {
    error.value = String(failure);
  } finally {
    busy.value = false;
  }
}
</script>
<template>
  <WorkspaceDialog
    id="editor"
    :open="true"
    title="添加视频"
    :busy="busy"
    :before-close="confirmNavigation"
    @close="emit('close')"
    ><p>{{ job.customer }} / {{ job.name }} · 添加后不会自动开始尚未开始的任务。</p>
    <form @submit.prevent="submit">
      <fieldset id="video-fields" :disabled="busy">
        <label class="field"
          >视频 ID 或 YouTube 链接<textarea
            v-model="urls"
            name="ids"
            rows="6"
            placeholder="每行一条视频 ID 或链接"
          ></textarea></label
        ><label class="field"
          >导入 TXT / CSV<input
            type="file"
            name="file"
            accept=".txt,.csv"
            @change="file = ($event.target as HTMLInputElement).files?.[0]"
        /></label>
        <p v-if="summary" role="status">{{ summary }}</p>
        <p v-if="error" role="alert" class="form-error">{{ error }}</p>
        <button type="submit">{{ busy ? '正在提交…' : '添加到任务' }}</button>
      </fieldset>
    </form>
  </WorkspaceDialog>
</template>
