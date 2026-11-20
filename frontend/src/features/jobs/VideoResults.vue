<script setup lang="ts">
import { computed, ref, watch } from 'vue';
import { ElMessageBox } from 'element-plus';
import { api } from '../../api';
import { bootstrap } from '../../bootstrap';
import WorkspaceDialog from '../../components/WorkspaceDialog.vue';
import ArtifactPreview from '../delivery/ArtifactPreview.vue';
import { outputLabels, type DeliveryArtifact } from '../delivery/model';
import { jobEndpoint, videoStates, formatGB } from './contracts';
import {
  expiry,
  requirementText,
  reviewLabels,
  type TestSample,
  type VideoResult,
} from './results';
import { useResource } from '../../composables/resource';
const props = defineProps<{ jobId: string; taskId: string }>();
const emit = defineEmits<{ close: []; changed: [] }>();
const resource = useResource<VideoResult>();
const result = resource.data,
  preview = ref<DeliveryArtifact>(),
  busy = ref(false),
  error = ref(''),
  message = ref('');
const admin = bootstrap().role === 'admin',
  editable = bootstrap().role !== 'viewer';
const endpoint = computed(() => jobEndpoint(props.jobId) + '/videos/' + props.taskId);
const files = computed(() => [
  ...(result.value?.samples.flatMap((s) => s.files) || []),
  ...(result.value?.files || []).filter((f) => f.available),
]);
async function refresh() {
  await resource.load((signal) => api.request(endpoint.value + '/results', { signal }));
}
watch(() => props.taskId, refresh, { immediate: true });
async function action(sample: TestSample, action: string) {
  if (action === 'delete') {
    try {
      await ElMessageBox.confirm(
        '只删除这份测试样本，保留校验结果、统计和人工验收记录。',
        '清理样本',
        { confirmButtonText: '确认清理', cancelButtonText: '取消' },
      );
    } catch {
      return;
    }
  }
  busy.value = true;
  error.value = '';
  try {
    const response = await api.request<{ message: string }>(
      jobEndpoint(props.jobId) + `/samples/${sample.id}/action`,
      {
        method: 'POST',
        body: { action, revision: sample.revision, confirmed: true },
      },
    );
    message.value = response.message;
    await refresh();
    emit('changed');
  } catch (failure) {
    error.value = String(failure);
  } finally {
    busy.value = false;
  }
}
async function resample() {
  try {
    await ElMessageBox.confirm(
      '将按原交付要求重新下载测试内容并尝试留样，会产生网络请求。空间保护、数量和容量上限仍生效；任务暂停时先排队。',
      '重新下载并留样',
      { confirmButtonText: '确认重新下载', cancelButtonText: '取消' },
    );
  } catch {
    return;
  }
  busy.value = true;
  error.value = '';
  try {
    const response = await api.request<{ message: string }>(endpoint.value + '/resample', {
      method: 'POST',
      body: { confirmed: true },
    });
    message.value = response.message;
    await refresh();
    emit('changed');
  } catch (failure) {
    error.value = String(failure);
  } finally {
    busy.value = false;
  }
}
</script>
<template>
  <WorkspaceDialog :open="true" title="视频结果与文件" :busy="busy" @close="emit('close')">
    <p v-if="resource.loading.value && !result">正在读取结果…</p>
    <p v-if="resource.error.value || error" class="form-error" role="alert">
      {{ error || resource.error.value }}
    </p>
    <p v-if="message" class="notice" role="status">{{ message }}</p>
    <template v-if="result">
      <h3>{{ result.title || result.video_id }}</h3>
      <p>
        {{ result.video_id }} · 执行状态：{{ videoStates[result.status] || result.status }} ·
        {{ result.delivery.test_mode ? '测试处理，不计费' : '正式交付' }}
      </p>
      <p v-if="result.error" class="form-error">{{ result.error }}</p>
      <p v-if="result.retention" class="notice">{{ result.retention.message }}</p>
      <p v-if="result.review">
        {{
          reviewLabels[result.review.status as keyof typeof reviewLabels] || result.review.status
        }}
        · {{ result.review.actor }} · {{ new Date(result.review.time * 1000).toLocaleString() }}
      </p>
      <h3>所选内容的实际结果</h3>
      <div v-for="(label, kind) in outputLabels" :key="kind" class="result-kind">
        <strong>{{ label }}</strong>
        <span v-if="!result.selected_outputs.includes(kind)">未选择</span>
        <template v-else>
          <p
            v-for="requirement in result.delivery.requirements.filter((r) => r.kind === kind)"
            :key="requirement.key"
          >
            {{ requirement.language }}：{{
              requirementText(
                requirement.status,
                files.some((f) => f.id === requirement.artifact_id),
                !!result.delivery.test_mode,
              )
            }}
            <small v-if="requirement.error" class="cell-note">{{ requirement.error }}</small>
          </p>
          <span v-if="!result.delivery.requirements.some((r) => r.kind === kind)"
            >等待解析和处理</span
          >
          <div
            v-for="file in files.filter((f) => f.kind === kind)"
            :key="file.content_url"
            class="detail-row"
          >
            <span
              >{{ file.language }} {{ formatGB(file.size) }} ·
              {{ file.storage === 'test' ? '测试留样' : file.storage === 'oss' ? 'OSS' : '本地' }} ·
              {{ file.instance_id }}</span
            >
            <button @click="preview = file">
              {{ ['subtitle', 'metadata'].includes(kind) ? '查看全文' : '预览 / 下载' }}
            </button>
          </div>
        </template>
      </div>
      <p v-if="!files.length" class="notice">
        当前没有可查看的文件。已清理的测试文件不会影响校验结果；需要核对内容时，可重新下载并留样。
      </p>
      <section v-for="sample in result.samples" :key="sample.id" class="card sample-card">
        <h3>测试样本 · {{ reviewLabels[sample.review] }}</h3>
        <p>
          {{ expiry(sample.expires) }} · {{ formatGB(sample.size) }} · 实例 {{ sample.instance_id }}
        </p>
        <p>
          空间紧张时可能提前回收。查看中的文件会短暂保护；关闭预览后最多等待 2 分钟即可手动清理。
        </p>
        <div class="actions">
          <button :disabled="!editable || busy" @click="action(sample, 'accept')">验收通过</button>
          <button :disabled="!editable || busy" @click="action(sample, 'reject')">发现问题</button>
          <button :disabled="!admin || busy" @click="action(sample, 'extend')">
            再保留 24 小时
          </button>
          <button
            :disabled="!admin || busy || !!preview"
            class="danger"
            @click="action(sample, 'delete')"
          >
            清理这份样本
          </button>
        </div>
      </section>
      <button
        v-if="result.delivery.test_mode"
        :disabled="
          !editable || busy || !['succeeded', 'failed', 'interrupted'].includes(result.status)
        "
        @click="resample"
      >
        重新下载并留样
      </button>
      <p v-else class="muted">文件读取失败时，可关闭此窗口，在视频清单中点击“补交缺项”。</p>
    </template>
    <ArtifactPreview
      v-if="preview"
      :key="preview.content_url"
      :artifact="preview"
      @close="preview = undefined"
    />
  </WorkspaceDialog>
</template>
<style scoped>
.result-kind {
  padding: 14px 0;
  border-top: 1px solid var(--line);
}
.result-kind > strong {
  display: inline-block;
  min-width: 100px;
}
.sample-card {
  margin: 18px 0;
}
</style>
