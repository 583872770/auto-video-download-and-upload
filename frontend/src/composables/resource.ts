import { onUnmounted, ref, shallowRef } from 'vue';

/** A late response must never replace a newer filter, page or task selection. */
export function useResource<T>() {
  const data = shallowRef<T>(),
    error = ref(''),
    loading = ref(false);
  let controller: AbortController | undefined,
    revision = 0;
  async function load(read: (signal: AbortSignal) => Promise<T>, signal?: AbortSignal) {
    controller?.abort();
    const current = new AbortController(),
      ticket = ++revision;
    controller = current;
    const abort = () => current.abort();
    signal?.addEventListener('abort', abort, { once: true });
    if (signal?.aborted) abort();
    loading.value = true;
    try {
      const value = await read(current.signal);
      if (ticket === revision && !current.signal.aborted) {
        data.value = value;
        error.value = '';
      }
    } catch (failure) {
      if (ticket === revision && !current.signal.aborted)
        error.value = failure instanceof Error ? failure.message : String(failure);
    } finally {
      signal?.removeEventListener('abort', abort);
      if (ticket === revision) loading.value = false;
    }
  }
  function clear() {
    controller?.abort();
    revision++;
    loading.value = false;
    data.value = undefined;
    error.value = '';
  }
  onUnmounted(clear);
  return { data, error, loading, load, clear };
}
