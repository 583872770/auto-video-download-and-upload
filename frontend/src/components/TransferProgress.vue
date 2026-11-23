<script setup lang="ts">
import type { VideoTask } from '../features/jobs/contracts';
import { diskBytes } from '../features/disk/model';
withDefaults(defineProps<{ progress?: VideoTask['progress']; showPercent?: boolean }>(), {
  showPercent: true,
});
</script>
<template>
  <span v-if="showPercent">
    <template v-if="progress?.stage === 'downloading'">当前文件 </template>
    {{ progress?.percent == null ? '—' : progress.percent.toFixed(1) + '%' }}
  </span>
  <small
    v-if="progress?.stage === 'downloading' && progress.current_downloaded_bytes != null"
    class="cell-note"
  >
    已下载 {{ diskBytes(progress.current_downloaded_bytes) }}
    <template v-if="progress.current_total_bytes != null">
      / {{ progress.current_total_estimated ? '约 ' : ''
      }}{{ diskBytes(progress.current_total_bytes) }}
    </template>
    <template v-if="progress.speed != null && Number.isFinite(progress.speed)">
      · {{ (progress.speed / 1e6).toFixed(2) }} MB/s
    </template>
    <template v-if="progress.eta != null && Number.isFinite(progress.eta)">
      · 剩余约 {{ Math.ceil(progress.eta) }} 秒
    </template>
  </small>
</template>
