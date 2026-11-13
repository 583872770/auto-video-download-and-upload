import { onMounted, onUnmounted, watch, type Ref } from 'vue';

/** One in-flight request per owner. Pause hidden pages and dispose on unmount. */
export function usePolling(
  active: Ref<boolean>,
  task: (signal: AbortSignal) => Promise<void>,
  interval = 10000,
) {
  let timer: ReturnType<typeof setTimeout> | undefined;
  let controller: AbortController | undefined;
  let failures = 0;
  let mounted = false;
  async function run() {
    clearTimeout(timer);
    if (!mounted || !active.value || document.hidden || controller) return;
    const current = new AbortController();
    controller = current;
    try {
      await task(current.signal);
      failures = 0;
    } catch (error) {
      if (!(error instanceof DOMException && error.name === 'AbortError')) failures++;
    } finally {
      if (controller === current) controller = undefined;
      if (mounted && active.value && !document.hidden)
        timer = setTimeout(run, Math.min(60000, interval * 2 ** Math.min(failures, 3)));
    }
  }
  function stop() {
    clearTimeout(timer);
    controller?.abort();
  }
  const visibility = () => (document.hidden ? stop() : void run());
  watch(active, (value) => (value ? void run() : stop()));
  onMounted(() => {
    mounted = true;
    document.addEventListener('visibilitychange', visibility);
    void run();
  });
  onUnmounted(() => {
    mounted = false;
    stop();
    document.removeEventListener('visibilitychange', visibility);
  });
  return { refresh: run };
}
