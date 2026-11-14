<script setup lang="ts">
import { ref } from 'vue';
import { api } from '../../api';
import { useLeaveGuard } from '../../composables/leaveGuard';
import WorkspaceDialog from '../../components/WorkspaceDialog.vue';
import { jobEndpoint, type Job } from './contracts';
const props = defineProps<{ job: Job; action: 'clear' | 'delete' }>();
const emit = defineEmits<{ close: []; completed: [] }>();
const confirmation = ref(''),
  scope = ref('pending'),
  error = ref(''),
  busy = ref(false);
useLeaveGuard(() => ({ dirty: false, busy: busy.value }));
async function submit() {
  if (busy.value) return;
  if (confirmation.value !== props.job.name) {
    error.value = '请输入完整任务名称确认。';
    return;
  }
  busy.value = true;
  error.value = '';
  try {
    await api.request(
      jobEndpoint(props.job.id) + (props.action === 'delete' ? '/control' : '/videos/remove'),
      {
        method: 'POST',
        body:
          props.action === 'delete'
            ? { action: 'delete', confirmation: confirmation.value }
            : { clear: true, scope: scope.value, confirmation: confirmation.value },
      },
    );
    busy.value = false;
    emit('completed');
  } catch (failure) {
    error.value = String(failure);
  } finally {
    busy.value = false;
  }
}
</script>
<template>
  <WorkspaceDialog
    id="clear-dialog"
    :open="true"
    :title="action === 'delete' ? '删除任务' : '清空当前任务视频清单'"
    :busy="busy"
    @close="emit('close')"
  >
    <p class="notice">
      {{
        action === 'delete'
          ? '删除会取消此任务的未完成下载和导入，并清除工作台关联，历史账单和已下载文件保留。删除后无法在任务中心恢复。'
          : '清空会暂停此任务并取消未完成导入，历史账单和磁盘文件保留。'
      }}
    </p>
    <form id="clear-form" @submit.prevent="submit">
      <fieldset :disabled="busy">
        <label class="field"
          ><span id="confirm-label"
            >输入任务名称确认：{{ job.name }}（客户：{{ job.customer }}）</span
          ><input v-model="confirmation" autocomplete="off"
        /></label>
        <label v-if="action === 'clear'" class="field"
          >清空范围<select v-model="scope">
            <option value="pending">未交付的视频</option>
            <option value="all">全部视频</option>
          </select></label
        >
        <p v-if="error" id="clear-error" role="alert" class="form-error">{{ error }}</p>
        <div class="actions">
          <button id="cancel-clear" type="button" @click="emit('close')">取消</button
          ><button type="submit" class="danger">
            {{ busy ? '正在提交…' : action === 'delete' ? '确认删除任务' : '确认清空视频' }}
          </button>
        </div>
      </fieldset>
    </form>
  </WorkspaceDialog>
</template>
