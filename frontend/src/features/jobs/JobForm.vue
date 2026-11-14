<script setup lang="ts">
import { computed, ref, toRaw } from 'vue';
import { api } from '../../api';
import { ApiError } from '../../api/client';
import { useLeaveGuard } from '../../composables/leaveGuard';
import DeliverySpecEditor from '../delivery/DeliverySpecEditor.vue';
import { defaultDelivery, validateDelivery } from '../delivery/model';
import { jobEndpoint, type Job } from './contracts';
const props = defineProps<{ job?: Job; disabled?: boolean }>();
const emit = defineEmits<{ saved: [id: string]; busy: [value: boolean] }>();
const draft = ref({
  customer: props.job?.customer || '',
  name: props.job?.name || '',
  note: props.job?.note || '',
  price: props.job?.price_set ? props.job.price : '',
  unit: props.job?.unit || 'GB',
  delivery: props.job?.delivery.contract
    ? structuredClone(toRaw(props.job.delivery.contract.spec))
    : defaultDelivery(),
  enableDelivery: !props.job || !!props.job.delivery.contract,
});
const baseline = ref(JSON.stringify(draft.value)),
  error = ref(''),
  saving = ref(false),
  latest = ref<Job>();
const version = ref(props.job?.edit_version || 0),
  requestId = Array.from(crypto.getRandomValues(new Uint8Array(16)), (value) =>
    value.toString(16).padStart(2, '0'),
  ).join('');
const dirty = computed(() => JSON.stringify(draft.value) !== baseline.value);
useLeaveGuard(() => ({ dirty: dirty.value, busy: saving.value }));
async function save() {
  if (saving.value || props.disabled) return;
  error.value = '';
  if (draft.value.enableDelivery) error.value = validateDelivery(draft.value.delivery);
  if (error.value) return;
  saving.value = true;
  emit('busy', true);
  let savedId = '';
  try {
    const { enableDelivery, delivery, ...fields } = draft.value;
    const response = await api.request<{ id: string; edit_version?: number }>(
      props.job ? jobEndpoint(props.job.id) : '/api/jobs',
      {
        method: props.job ? 'PUT' : 'POST',
        body: {
          ...fields,
          ...(enableDelivery ? { delivery } : {}),
          ...(props.job ? { edit_version: version.value } : { request_id: requestId }),
        },
      },
    );
    baseline.value = JSON.stringify(draft.value);
    latest.value = undefined;
    if (response.edit_version !== undefined) version.value = response.edit_version;
    savedId = response.id;
  } catch (failure) {
    error.value = failure instanceof Error ? failure.message : String(failure);
    if (failure instanceof ApiError && failure.status === 409 && props.job) {
      try {
        latest.value = await api.request<Job>(jobEndpoint(props.job.id));
      } catch {
        /* Retain the original conflict and every draft field. */
      }
    }
  } finally {
    saving.value = false;
    emit('busy', false);
  }
  if (savedId) emit('saved', savedId);
}
function acceptVersion() {
  if (latest.value) {
    version.value = latest.value.edit_version;
    latest.value = undefined;
    error.value = '已采用最新编辑版本，请核对当前输入后再次保存。';
  }
}
defineExpose({ dirty, saving });
</script>
<template>
  <form :id="job ? 'settings-form' : 'editor-form'" @submit.prevent="save">
    <fieldset :disabled="disabled || saving">
      <div v-if="latest" class="notice" data-conflict>
        <p>
          服务器最新资料：客户 {{ latest.customer }}，名称 {{ latest.name }}，单价
          {{ latest.price_set ? latest.price + ' 元 / ' + latest.unit : '待定' }}，备注
          {{ latest.note || '无' }}。下方仍保留你的输入。
        </p>
        <button type="button" @click="acceptVersion">已核对，保留当前输入继续编辑</button>
      </div>
      <div class="fields" id="basic-fields">
        <label class="field"
          >客户 *<input v-model="draft.customer" name="customer" required maxlength="200"
        /></label>
        <label class="field"
          >任务名称 *<input v-model="draft.name" name="name" required maxlength="200"
        /></label>
        <label class="field"
          >单价（可留空）<input
            v-model="draft.price"
            name="price"
            type="number"
            min="0"
            max="1000000"
            step="0.000001"
          /><small>留空待定，0 表示免费。</small></label
        >
        <label class="field"
          >计费单位<select v-model="draft.unit" name="unit">
            <option value="GB">GB（10⁹ 字节）</option>
            <option value="GiB">GiB（2³⁰ 字节）</option>
          </select></label
        >
      </div>
      <label class="field"
        >备注<textarea v-model="draft.note" name="note" maxlength="2000"></textarea>
      </label>
      <p v-if="job" class="notice">
        修改单价或单位会重算本任务全部交付的预计费用。下载策略由执行实例提供。
      </p>
      <label v-if="job && !job.delivery.contract" class="choice"
        ><input
          v-model="draft.enableDelivery"
          type="checkbox"
          name="delivery-enable"
        />为尚未开始的来源启用多类型交付</label
      >
      <section v-if="draft.enableDelivery" class="delivery-task-section">
        <div class="section-heading">
          <div>
            <h3>本任务交付内容</h3>
            <p class="muted">在这里指定本任务需要交付的文件类型、画质和字幕要求。</p>
          </div>
        </div>
        <DeliverySpecEditor v-model="draft.delivery" />
      </section>
      <p v-if="job" class="muted">
        已开始的来源沿用固定要求版本；补交可以显式采用最新要求。交付要求可随时在此任务中调整。
      </p>
      <p v-if="error" :id="job ? 'settings-error' : 'editor-error'" class="form-error" role="alert">
        {{ error }}
      </p>
      <button class="primary" type="submit" :disabled="saving || disabled">
        {{ saving ? '正在保存…' : job ? '保存任务设置' : '创建任务' }}
      </button>
    </fieldset>
  </form>
</template>
