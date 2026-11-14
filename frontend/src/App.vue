<script setup lang="ts">
import { RouterView, useRoute } from 'vue-router';
import { onMounted, onUnmounted } from 'vue';
import { hasPendingEdits } from './composables/leaveGuard';
import WorkspaceShell from './components/WorkspaceShell.vue';
const route = useRoute();
function beforeUnload(event: BeforeUnloadEvent) {
  if (route.meta.standalone && hasPendingEdits()) {
    event.preventDefault();
    event.returnValue = '';
  }
}
onMounted(() => window.addEventListener('beforeunload', beforeUnload));
onUnmounted(() => window.removeEventListener('beforeunload', beforeUnload));
</script>
<template>
  <RouterView v-if="route.meta.standalone" v-slot="{ Component }"
    ><component :is="Component" :key="route.path + String(route.query.instance_id || '')"
  /></RouterView>
  <WorkspaceShell v-else
    ><RouterView v-slot="{ Component }"
      ><component
        :is="Component"
        :key="route.path + String(route.query.instance_id || '')" /></RouterView
  ></WorkspaceShell>
</template>
