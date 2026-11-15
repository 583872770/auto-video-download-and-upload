<script setup lang="ts">
import { onMounted } from 'vue';
import { useInstances } from '../stores/instances';
import { bootstrap } from '../bootstrap';
import ScopePanel from './ScopePanel.vue';
const props = withDefaults(
  defineProps<{
    disabled?: boolean;
    purpose?: 'view' | 'configure' | 'operate';
    description?: string;
  }>(),
  { purpose: 'configure' },
);
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
    <ScopePanel
      level="instance"
      :purpose="purpose"
      :target="instances.name"
      :description="
        description ||
        (purpose === 'view'
          ? '展示所选实例的数据；切换实例只改变查看范围。'
          : purpose === 'operate'
            ? '选择任务、启动与暂停操作作用于此实例。任务的交付和保存规则在任务中心配置。'
            : '修改仅作用于此实例；其他实例保持各自配置。')
      "
    >
      <label v-if="bootstrap().fleetEnabled" class="instance-picker"
        >{{ purpose === 'view' ? '查看实例' : '切换实例'
        }}<select
          aria-label="当前实例"
          :value="instances.selectedId"
          :disabled="props.disabled || instances.loading"
          @change="select"
        >
          <option v-for="item in instances.items" :key="item.id" :value="item.id">
            {{ item.name }}{{ item.online ? '' : '（离线）' }}
          </option>
        </select></label
      >
    </ScopePanel>
    <p v-if="instances.error" role="alert" class="form-error">
      {{ instances.error }}
      <button @click="instances.load().then(() => emit('change'))">重试</button>
    </p>
  </div>
</template>
