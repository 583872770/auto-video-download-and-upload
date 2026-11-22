import { onUnmounted, ref, shallowRef } from 'vue';

/** A late response must never replace a newer filter, page or task selection. */
export function useResource<T>() {
  const data = shallowRef<T>(),
    error = ref(''),
    loading = ref(false),
    lastSuccessAt = ref(0),
    lastAttemptAt = ref(0),
    consecutiveFailures = ref(0);
  let controller: AbortController | undefined,
    revision = 0;
  async function load(
    read: (signal: AbortSignal) => Promise<T>,
    signal?: AbortSignal,
  ): Promise<boolean | undefined> {
    controller?.abort();
    const current = new AbortController(),
      ticket = ++revision;
    controller = current;
    const abort = () => current.abort();
    signal?.addEventListener('abort', abort, { once: true });
    if (signal?.aborted) abort();
    loading.value = true;
    lastAttemptAt.value = Date.now() / 1000;
    try {
      const value = await read(current.signal);
      if (ticket === revision && !current.signal.aborted) {
        data.value = value;
        error.value = '';
        lastSuccessAt.value = Date.now() / 1000;
        consecutiveFailures.value = 0;
        return true;
      }
      return undefined;
    } catch (failure) {
      if (ticket === revision && !current.signal.aborted) {
        error.value = failure instanceof Error ? failure.message : String(failure);
        consecutiveFailures.value++;
        return false;
      }
      return undefined;
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
    lastSuccessAt.value = 0;
    lastAttemptAt.value = 0;
    consecutiveFailures.value = 0;
  }
  onUnmounted(clear);
  return {
    data,
    error,
    loading,
    lastSuccessAt,
    lastAttemptAt,
    consecutiveFailures,
    load,
    clear,
  };
}
