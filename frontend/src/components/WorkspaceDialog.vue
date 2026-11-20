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
let backdropPressed = false;
let closing = false;
function synchronize() {
  if (props.open && !element.value?.open) element.value?.showModal();
  else if (!props.open && element.value?.open) element.value.close();
}
async function close() {
  if (props.busy || closing) return;
  closing = true;
  try {
    if (props.beforeClose && !(await props.beforeClose())) return;
    emit('close');
  } finally {
    closing = false;
  }
}
function onBackdrop(event: MouseEvent) {
  const dialog = element.value;
  if (!dialog || event.target !== dialog) return false;
  const rect = dialog.getBoundingClientRect();
  return (
    event.clientX < rect.left ||
    event.clientX > rect.right ||
    event.clientY < rect.top ||
    event.clientY > rect.bottom
  );
}
function pointerDown(event: PointerEvent) {
  backdropPressed = event.button === 0 && onBackdrop(event);
}
function click(event: MouseEvent) {
  const dismiss = backdropPressed && onBackdrop(event);
  backdropPressed = false;
  if (dismiss) void close();
}
onMounted(synchronize);
watch(() => props.open, synchronize, { flush: 'post' });
</script>
<template>
  <dialog
    ref="element"
    class="modal"
    :aria-label="title"
    @cancel.prevent="close"
    @pointerdown="pointerDown"
    @pointercancel="backdropPressed = false"
    @click="click"
  >
    <div class="modal-heading">
      <h2>{{ title }}</h2>
      <button type="button" :disabled="busy" aria-label="关闭对话框" @click="close">关闭</button>
    </div>
    <slot />
  </dialog>
</template>
