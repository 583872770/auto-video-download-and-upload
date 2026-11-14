<script setup lang="ts">
import { computed, onMounted, ref } from 'vue';
import { api } from '../../api';
import { bootstrap } from '../../bootstrap';
import { ElButton } from 'element-plus';
import 'element-plus/es/components/button/style/css';
import DeliverySpecEditor from '../delivery/DeliverySpecEditor.vue';
import { validateDelivery, type DeliverySpec as Spec } from '../delivery/model';
const spec = ref<Spec>(),
  baseline = ref(''),
  loading = ref(true),
  saving = ref(false),
  error = ref(''),
  message = ref('');
const dirty = computed(() => !!spec.value && JSON.stringify(spec.value) !== baseline.value);
const admin = bootstrap().role === 'admin';
const labels = { subtitle: '字幕', video_only: '无声视频', audio_only: '纯音频', av: '音视频成品' };
function accept(value: Spec) {
  spec.value = value;
  baseline.value = JSON.stringify(value);
}
async function load() {
  loading.value = true;
  error.value = '';
  try {
    accept(
      (await api.request<{ spec: Spec }>('/api/delivery/defaults', { cache: 'no-store' })).spec,
    );
  } catch (failure) {
    error.value = String(failure);
  } finally {
    loading.value = false;
  }
}
async function save() {
  if (!spec.value || !dirty.value || saving.value || !admin) return;
  error.value = '';
  message.value = '';
  error.value = validateDelivery(spec.value);
  if (error.value) return;
  saving.value = true;
  const submitted = JSON.stringify(spec.value);
  try {
    const result = await api.request<{ spec: Spec }>('/api/delivery/defaults', {
      method: 'PUT',
      body: JSON.parse(submitted),
    });
    const saved = await api.request<{ spec: Spec }>('/api/delivery/defaults', {
      cache: 'no-store',
    });
    if (JSON.stringify(saved.spec) !== JSON.stringify(result.spec))
      throw new Error('读回结果发生变化，请重新读取核对。');
    accept(saved.spec);
    message.value = '新任务默认要求已保存，仅影响之后创建的任务。';
  } catch (failure) {
    error.value = '保存结果尚未确认：' + String(failure) + '；修改已保留，可重新读取核对。';
  } finally {
    saving.value = false;
  }
}
function reset() {
  if (baseline.value) accept(JSON.parse(baseline.value));
  message.value = '';
  error.value = '';
}
onMounted(load);
defineExpose({ dirty, saving });
</script>
<template>
  <section class="card delivery-defaults">
    <h2>新任务交付默认</h2>
    <p>这是整个工作区的默认要求，独立保存。当前实例的其他设置不会改变这些默认值。</p>
    <p v-if="loading" role="status">正在读取默认要求…</p>
    <p v-if="error" class="error" role="alert">{{ error }}</p>
    <p v-if="message" class="success" role="status">{{ message }}</p>
    <button v-if="!spec && !loading" type="button" @click="load">重新读取</button>
    <form v-if="spec" novalidate @submit.prevent="save">
      <fieldset :disabled="!admin || loading || saving">
        <DeliverySpecEditor v-model="spec" />
        <div class="actions">
          <ElButton type="primary" native-type="submit" :disabled="!dirty" :loading="saving"
            >保存新任务默认要求</ElButton
          ><ElButton :disabled="!dirty" @click="reset">放弃修改</ElButton>
        </div>
      </fieldset>
    </form>
  </section>
</template>
