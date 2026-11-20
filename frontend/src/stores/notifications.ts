import { ref, shallowRef } from 'vue';
import { defineStore } from 'pinia';
import { api } from '../api';
import type {
  Notification,
  NoticeCounts,
  NoticePage,
  NoticeState,
} from '../features/notifications/model';

export const useNotifications = defineStore('notifications', () => {
  const open = ref(false),
    state = ref<NoticeState>('action');
  const instance = ref(''),
    job = ref('');
  const counts = ref<NoticeCounts>({ action: 0, recovering: 0, resolved: 0 });
  const globalCounts = ref<NoticeCounts>({ action: 0, recovering: 0, resolved: 0 });
  const items = shallowRef<Notification[]>([]),
    nextCursor = ref<string | null>(null);
  const resourceAlerts = shallowRef<Notification[]>([]);
  const loading = ref(false),
    error = ref(''),
    sampledAt = ref(0);
  let paginated = false;
  let controller: AbortController | undefined,
    revision = 0;
  async function refresh(signal?: AbortSignal, append = false) {
    if (append && (!nextCursor.value || loading.value)) return;
    controller?.abort();
    const current = new AbortController(),
      ticket = ++revision;
    controller = current;
    const abort = () => current.abort();
    signal?.addEventListener('abort', abort, { once: true });
    if (signal?.aborted) abort();
    const expanded = open.value;
    const loadList = expanded && (!paginated || append);
    const params = new URLSearchParams({ state: state.value });
    if (!loadList) params.set('compact', '1');
    if (expanded) {
      if (instance.value) params.set('instance_id', instance.value);
      if (job.value) params.set('job_id', job.value);
      if (append && nextCursor.value) params.set('cursor', nextCursor.value);
    }
    loading.value = true;
    try {
      // Intentionally global: never send the top bar's selected-instance header.
      const value = await api.request<NoticePage>('/api/notifications?' + params, {
        signal: current.signal,
        timeoutMs: 30000,
      });
      if (ticket !== revision || current.signal.aborted) return;
      globalCounts.value = value.global_counts;
      resourceAlerts.value = value.resource_alerts || [];
      sampledAt.value = value.sampled_at;
      if (expanded) {
        counts.value = value.counts;
        if (loadList) {
          const merged = append ? [...items.value, ...value.items] : value.items;
          items.value = [...new Map(merged.map((item) => [item.id, item])).values()];
          nextCursor.value = value.next_cursor;
          if (append) paginated = true;
        }
      }
      error.value = '';
    } catch (failure) {
      if (ticket === revision && !current.signal.aborted)
        error.value = failure instanceof Error ? failure.message : String(failure);
    } finally {
      signal?.removeEventListener('abort', abort);
      if (ticket === revision) loading.value = false;
      if (controller === current) controller = undefined;
    }
  }
  function show() {
    paginated = false;
    open.value = true;
    void refresh();
  }
  async function reload() {
    paginated = false;
    await refresh();
  }
  function filter() {
    paginated = false;
    items.value = [];
    nextCursor.value = null;
    void refresh();
  }
  function close() {
    open.value = false;
  }
  function dispose() {
    revision++;
    controller?.abort();
    controller = undefined;
  }
  return {
    open,
    state,
    instance,
    job,
    counts,
    globalCounts,
    items,
    resourceAlerts,
    nextCursor,
    loading,
    error,
    sampledAt,
    refresh,
    reload,
    show,
    filter,
    close,
    dispose,
  };
});
