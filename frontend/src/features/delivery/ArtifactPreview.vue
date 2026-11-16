<script setup lang="ts">
import { computed, onBeforeUnmount, ref } from 'vue';
import WorkspaceDialog from '../../components/WorkspaceDialog.vue';
import { useResource } from '../../composables/resource';
import { api } from '../../api';
import type { DeliveryArtifact } from './model';
import { subtitleDescription } from './presentation';
const props = defineProps<{ artifact: DeliveryArtifact }>();
const emit = defineEmits<{ close: [] }>();
const resource = useResource<string>(),
  mediaError = ref('');
const media = ref<HTMLMediaElement>();
onBeforeUnmount(() => {
  if (media.value) {
    media.value.pause();
    media.value.removeAttribute('src');
    media.value.load();
  }
});
const url = computed(() => '/api/delivery/files/' + encodeURIComponent(props.artifact.id));
const textFile = ['subtitle', 'metadata'].includes(props.artifact.kind);
const tooLarge = props.artifact.size > 2 * 1024 * 1024;
const metadataLabels: Record<string, string> = {
  id: '视频 ID',
  title: '标题',
  description: '简介',
  channel: '频道',
  channel_id: '频道 ID',
  uploader: '发布者',
  uploader_id: '发布者 ID',
  upload_date: '发布日期',
  duration: '时长（秒）',
  timestamp: '发布时间戳',
  language: '语言',
  license: '许可',
  availability: '可用状态',
  view_count: '播放次数',
  like_count: '点赞数',
  comment_count: '评论数',
  tags: '标签',
  categories: '分类',
  chapters: '章节',
};
const metadata = computed(() => {
  if (props.artifact.kind !== 'metadata' || !resource.data.value) return [];
  return Object.entries(JSON.parse(resource.data.value) as Record<string, unknown>)
    .filter(([key]) => key !== 'schema_version')
    .map(([key, value]) => ({
      label: metadataLabels[key] || key,
      value: typeof value === 'object' ? JSON.stringify(value, null, 2) : String(value),
    }));
});
if (textFile && !tooLarge)
  void resource.load(async (signal) => {
    const text = await api.request<string>(url.value, { signal, responseType: 'text' });
    if (props.artifact.kind === 'metadata') JSON.parse(text);
    return text;
  });
</script>
<template>
  <WorkspaceDialog :open="true" :title="artifact.title || artifact.video_id" @close="emit('close')"
    ><p>{{ artifact.video_id }}</p>
    <template v-if="textFile">
      <p v-if="artifact.kind === 'subtitle'" class="notice">{{ subtitleDescription(artifact) }}</p>
      <p v-if="tooLarge" class="notice">文件较大，请下载后查看完整内容。</p>
      <dl v-else-if="artifact.kind === 'metadata' && resource.data.value" class="metadata-details">
        <div v-for="field in metadata" :key="field.label">
          <dt>{{ field.label }}</dt>
          <dd>{{ field.value }}</dd>
        </div>
      </dl>
      <pre v-else-if="resource.data.value">{{ resource.data.value }}</pre>
      <p v-else-if="resource.loading.value" role="status">正在读取文件…</p>
      <p v-if="resource.error.value" role="alert" class="form-error">
        {{ resource.error.value }}
      </p></template
    ><img
      v-else-if="artifact.kind === 'thumbnail'"
      :src="url"
      :alt="artifact.title + '的封面图'"
      class="cover-preview"
      @error="mediaError = '封面图读取失败，可尝试下载文件。'"
    />
    <component
      :is="artifact.kind === 'audio_only' ? 'audio' : 'video'"
      ref="media"
      v-else
      :src="url"
      controls
      preload="metadata"
      @error="mediaError = '文件不可读取或浏览器不支持此编码，可下载后播放。'"
    />
    <p v-if="mediaError" role="alert" class="form-error">{{ mediaError }}</p>
    <a class="text-link" :href="url + '?download=1'">下载文件</a></WorkspaceDialog
  >
</template>
<style scoped>
.cover-preview {
  display: block;
  max-width: 100%;
  max-height: 65vh;
  margin: 12px auto;
  object-fit: contain;
  border-radius: 8px;
}
.metadata-details {
  margin: 16px 0;
}
.metadata-details > div {
  display: grid;
  grid-template-columns: 100px minmax(0, 1fr);
  gap: 12px;
  padding: 12px 0;
  border-top: 1px solid var(--line);
}
.metadata-details dt {
  color: var(--muted);
  font-size: 13px;
}
.metadata-details dd {
  margin: 0;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
@media (max-width: 600px) {
  .metadata-details > div {
    grid-template-columns: 1fr;
    gap: 6px;
  }
}
</style>
