<script setup lang="ts">
import { onMounted, ref, watch } from 'vue';
const props = defineProps<{
  open: boolean;
  title: string;
  busy?: boolean;
  beforeClose?: () => Promise<boolean>;
}>();
const emit = defineEmits<{ close: [] }>();
const element = ref<HTMLDialogElement>();
function synchronize() {
  if (props.open && !element.value?.open) element.value?.showModal();
  else if (!props.open && element.value?.open) element.value.close();
}
async function close() {
  if (props.busy || (props.beforeClose && !(await props.beforeClose()))) return;
  emit('close');
}
onMounted(synchronize);
watch(() => props.open, synchronize, { flush: 'post' });
</script>
<template>
  <dialog ref="element" class="modal" :aria-label="title" @cancel.prevent="close">
    <div class="modal-heading">
      <h2>{{ title }}</h2>
      <button type="button" :disabled="busy" aria-label="关闭对话框" @click="close">关闭</button>
    </div>
    <slot />
  </dialog>
</template>
