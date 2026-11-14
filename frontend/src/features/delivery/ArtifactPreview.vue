<script setup lang="ts">
import { computed, onBeforeUnmount, ref } from 'vue';
import WorkspaceDialog from '../../components/WorkspaceDialog.vue';
import { useResource } from '../../composables/resource';
import { api } from '../../api';
import type { DeliveryArtifact } from './model';
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
if (props.artifact.kind === 'subtitle')
  void resource.load(async (signal) =>
    (await api.request<string>(url.value, { signal, responseType: 'text' })).slice(0, 1024 * 1024),
  );
</script>
<template>
  <WorkspaceDialog :open="true" :title="artifact.title || artifact.video_id" @close="emit('close')"
    ><p>{{ artifact.video_id }}</p>
    <template v-if="artifact.kind === 'subtitle'">
      <pre v-if="resource.data.value">{{ resource.data.value }}</pre>
      <p v-else-if="resource.loading.value" role="status">正在读取字幕…</p>
      <p v-if="resource.error.value" role="alert" class="form-error">
        {{ resource.error.value }}
      </p></template
    ><component
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
