<script setup lang="ts">
import { computed } from 'vue';
import { RouterLink } from 'vue-router';
import { api } from '../api';
import { usePolling } from '../composables/polling';
import { useResource } from '../composables/resource';
const resource = useResource<{
  blockers: Array<{ code: string; message: string; href?: string; label?: string }>;
}>();
const blockers = computed(() =>
  (resource.data.value?.blockers || []).filter(
    (item) => !['system_paused', 'jobs_not_started', 'no_pending_videos'].includes(item.code),
  ),
);
usePolling(
  computed(() => true),
  (signal) => resource.load((signal) => api.request('/api/jobs/system/status', { signal }), signal),
  15000,
);
</script>
<template>
  <p v-if="blockers.length" class="notice" role="status">
    <template v-for="(blocker, index) in blockers" :key="index"
      >{{ blocker.message }}
      <a v-if="blocker.href" class="text-link" :href="blocker.href"
        >{{ blocker.label || '查看详情' }} ↗</a
      > </template
    ><RouterLink class="text-link" to="/workbench">前往下载工作台 ↗</RouterLink>
  </p>
  <p v-if="resource.error.value" class="form-error" role="alert">
    执行条件读取失败：{{ resource.error.value }}
  </p>
</template>
