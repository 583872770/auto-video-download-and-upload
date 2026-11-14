import { ref } from 'vue';
export function useOperation() {
  const busy = ref(false),
    error = ref(''),
    message = ref('');
  async function run(action: () => Promise<void>) {
    if (busy.value) return;
    busy.value = true;
    error.value = '';
    message.value = '';
    try {
      await action();
    } catch (failure) {
      if (!(failure instanceof DOMException && failure.name === 'AbortError'))
        error.value = failure instanceof Error ? failure.message : String(failure);
    } finally {
      busy.value = false;
    }
  }
  return { busy, error, message, run };
}
