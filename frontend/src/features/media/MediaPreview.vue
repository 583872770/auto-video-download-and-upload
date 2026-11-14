<script setup lang="ts">
import { onBeforeUnmount, ref } from 'vue';
import WorkspaceDialog from '../../components/WorkspaceDialog.vue';
import { mediaAddress, type MediaItem } from './contracts';
defineProps<{ item: MediaItem }>();
const emit = defineEmits<{ close: [] }>();
const player = ref<HTMLVideoElement>(),
  error = ref('');
onBeforeUnmount(() => {
  if (player.value) {
    player.value.pause();
    player.value.removeAttribute('src');
    player.value.load();
  }
});
</script>
<template>
  <WorkspaceDialog :open="true" :title="item.title || item.name" @close="emit('close')"
    ><p>{{ item.name }}</p>
    <video
      v-if="item.playable"
      ref="player"
      :src="mediaAddress('videos', item.name, item.instance_id)"
      :poster="item.cover ? mediaAddress('covers', item.cover, item.instance_id) : undefined"
      controls
      preload="metadata"
      @error="error = '浏览器无法播放此文件，可下载后查看，或检查实例连接。'"
    />
    <p v-else class="notice">音视频尚未合并，请检查下载任务日志和 FFmpeg 安装情况。</p>
    <p v-if="error" class="form-error" role="alert">{{ error }}</p>
    <a
      class="text-link"
      :href="mediaAddress('videos', item.name, item.instance_id)"
      :download="item.name"
      >下载文件</a
    ></WorkspaceDialog
  >
</template>
