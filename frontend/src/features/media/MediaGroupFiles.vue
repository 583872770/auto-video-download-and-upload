<script setup lang="ts">
import { onMounted, ref, watch } from 'vue';
import { api } from '../../api';
import { useInstances } from '../../stores/instances';
import { useResource } from '../../composables/resource';
import { subtitleDescription, subtitleLanguage } from '../delivery/presentation';
import type { DeliveryArtifact } from '../delivery/model';
import ArtifactPreview from '../delivery/ArtifactPreview.vue';
import MediaPreview from './MediaPreview.vue';
import {
  libraryTypeLabels,
  formatDownloadTime,
  formatFileSize,
  mediaAddress,
  type MediaGroup,
  type GroupFilesPage,
  type LibraryFile,
  type MediaItem,
} from './contracts';

const props = defineProps<{ group: MediaGroup }>();
const instances = useInstances();
const resource = useResource<GroupFilesPage>();
const cursor = ref<string | null>(null),
  history = ref<(string | null)[]>([]);
const preview = ref<MediaItem>(),
  artifactPreview = ref<DeliveryArtifact>();

async function refresh(signal?: AbortSignal) {
  const params = new URLSearchParams({ key: props.group.group_key, limit: '50' });
  if (cursor.value) params.set('cursor', cursor.value);
  const scope = instances.scope();
  await resource.load(
    (requestSignal) =>
      api.request('/api/media/group-files?' + params, { scope, signal: requestSignal }),
    signal,
  );
}
function page(next: boolean) {
  if (next && resource.data.value?.next_cursor) {
    history.value.push(cursor.value);
    cursor.value = resource.data.value.next_cursor;
  } else if (!next) cursor.value = history.value.pop() || null;
  void refresh();
}
function selectFile(file: LibraryFile) {
  if (file.local)
    preview.value = { ...file.local, instance_id: instances.selectedId || file.local.instance_id };
  else artifactPreview.value = file.artifact;
}
function fileUrl(file: LibraryFile) {
  return file.local
    ? mediaAddress('videos', file.local.name, instances.selectedId || file.local.instance_id)
    : file.artifact!.content_url + '?download=1';
}
onMounted(() => void refresh());
watch(
  () => [props.group.file_count, props.group.downloaded_at],
  () => void refresh(),
);
</script>
<template>
  <section class="group-files" :aria-label="group.title + '的文件'">
    <div class="files-heading">
      <strong
        >已下载文件 <span>{{ resource.data.value?.total ?? group.file_count }}</span></strong
      >
      <span class="cell-note">按类型及字幕语言列出，可分别查看和下载</span>
    </div>
    <p v-if="resource.error.value" class="form-error" role="alert">
      {{ resource.error.value }} <button @click="refresh()">重试</button>
    </p>
    <p v-if="resource.loading.value && !resource.data.value" role="status">正在读取文件…</p>
    <div v-for="file in resource.data.value?.items || []" :key="file.id" class="file-row">
      <div class="file-description">
        <span class="file-kind" :data-kind="file.kind">{{ libraryTypeLabels[file.kind] }}</span>
        <div>
          <strong>{{
            file.artifact?.kind === 'subtitle'
              ? subtitleLanguage(file.artifact.language)
              : libraryTypeLabels[file.kind]
          }}</strong>
          <small v-if="file.artifact?.kind === 'subtitle'" class="cell-note">{{
            subtitleDescription(file.artifact)
          }}</small>
          <small v-else-if="file.local" class="cell-note local-name">{{ file.local.name }}</small>
          <small v-else-if="file.kind === 'metadata'" class="cell-note"
            >JSON · 标题、简介、频道等资料</small
          >
          <small v-else-if="file.kind === 'thumbnail'" class="cell-note"
            >来源封面 · 保留原始图片格式</small
          >
          <small v-else class="cell-note">独立文件 · 可预览和下载</small>
        </div>
      </div>
      <span class="file-storage">{{
        file.artifact?.storage === 'oss' ? '云端 OSS' : '本地文件'
      }}</span>
      <span class="file-size">{{ formatFileSize(file.size) }}</span>
      <span class="file-time">{{ formatDownloadTime(file.downloaded_at) }}</span>
      <div class="actions file-actions">
        <button :data-preview="file.id" @click="selectFile(file)">
          {{
            file.kind === 'audio_only'
              ? '试听'
              : ['subtitle', 'metadata'].includes(file.kind)
                ? '查看'
                : '预览'
          }}
        </button>
        <a class="text-link" :href="fileUrl(file)">下载</a>
      </div>
    </div>
    <p v-if="resource.data.value && !resource.data.value.items.length" class="empty">
      本组文件已清理。
    </p>
    <div v-if="history.length || resource.data.value?.next_cursor" class="pager">
      <button :disabled="!history.length || resource.loading.value" @click="page(false)">
        上一页文件
      </button>
      <span>第 {{ history.length + 1 }} 页 · 共 {{ resource.data.value?.total }} 个文件</span>
      <button
        :disabled="!resource.data.value?.next_cursor || resource.loading.value"
        @click="page(true)"
      >
        下一页文件
      </button>
    </div>
    <MediaPreview v-if="preview" :item="preview" @close="preview = undefined" />
    <ArtifactPreview
      v-if="artifactPreview"
      :key="artifactPreview.id"
      :artifact="artifactPreview"
      @close="artifactPreview = undefined"
    />
  </section>
</template>
<style scoped>
.group-files {
  padding: 4px 12px 12px;
}
.files-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 8px 0 12px;
}
.files-heading strong {
  font-size: 13px;
}
.files-heading strong span {
  margin-left: 6px;
  color: #0f766e;
}
.file-row {
  display: grid;
  grid-template-columns: minmax(260px, 1fr) 90px 85px 170px 108px;
  align-items: center;
  gap: 16px;
  padding: 12px 0;
  border-top: 1px solid #e2e8f0;
}
.file-description {
  display: flex;
  align-items: center;
  gap: 12px;
  min-width: 0;
}
.file-description strong {
  font-size: 13px;
}
.file-description > div {
  min-width: 0;
}
.file-kind {
  flex-shrink: 0;
  min-width: 62px;
  padding: 6px 8px;
  border: 1px solid #cbd5e1;
  border-radius: 6px;
  color: #475569;
  text-align: center;
  font-size: 11px;
}
.file-kind[data-kind='subtitle'] {
  background: #ecfdf5;
  border-color: #a7f3d0;
  color: #047857;
}
.file-kind[data-kind='audio_only'] {
  background: #fffbeb;
  border-color: #fde68a;
  color: #92400e;
}
.file-kind[data-kind='thumbnail'] {
  background: #f5f3ff;
  border-color: #ddd6fe;
  color: #6d28d9;
}
.file-kind[data-kind='metadata'] {
  background: #f0fdfa;
  border-color: #99f6e4;
  color: #0f766e;
}
.file-storage,
.file-time {
  color: #64748b;
  font-size: 12px;
}
.file-size {
  font-variant-numeric: tabular-nums;
  font-size: 13px;
}
.local-name {
  overflow-wrap: anywhere;
}
.file-actions {
  justify-content: flex-end;
}
@media (max-width: 1000px) {
  .file-row {
    grid-template-columns: minmax(220px, 1fr) 80px 100px;
    gap: 10px;
  }
  .file-storage,
  .file-time {
    grid-row: auto;
  }
  .file-description {
    grid-column: 1;
    grid-row: span 2;
  }
  .file-actions {
    grid-column: 3;
    grid-row: span 2;
  }
  .file-time {
    grid-column: 2;
  }
  .files-heading {
    align-items: flex-start;
    flex-direction: column;
    gap: 4px;
  }
}
</style>
