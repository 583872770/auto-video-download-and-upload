<script setup lang="ts">
import { onMounted } from 'vue';
import { useInstances } from '../stores/instances';
import { bootstrap } from '../bootstrap';
const props = defineProps<{ disabled?: boolean }>();
const emit = defineEmits<{ change: [] }>();
const instances = useInstances();
onMounted(async () => {
  await instances.load();
  emit('change');
});
function select(event: Event) {
  instances.select((event.target as HTMLSelectElement).value);
  emit('change');
}
</script>
<template>
  <div class="instance-picker-container">
    <label v-if="bootstrap().fleetEnabled" class="instance-picker"
      >当前实例<select
        aria-label="当前实例"
        :value="instances.selectedId"
        :disabled="props.disabled || instances.loading"
        @change="select"
      >
        <option v-for="item in instances.items" :key="item.id" :value="item.id">
          {{ item.name }} · {{ item.id }}{{ item.online ? '' : '（离线）' }}
        </option>
      </select></label
    >
    <p v-if="instances.error" role="alert" class="form-error">
      {{ instances.error }}
      <button @click="instances.load().then(() => emit('change'))">重试</button>
    </p>
  </div>
</template>
