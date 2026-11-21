import { computed, ref, shallowRef, watch } from 'vue';
import { defineStore } from 'pinia';
import { api } from '../api';
import { bootstrap } from '../bootstrap';
import { useInstances } from './instances';
import type { RuntimeAccount, WorkbenchStatus } from '../features/workbench/contracts';
import { accountState } from '../features/workbench/accountState';

export interface HeaderStatus extends Omit<WorkbenchStatus, 'active_tasks'> {
  transfer_summary: { time: number; transferring: number; measured: number; speed: number };
  account_summary: { sample_time: number | null; accounts: RuntimeAccount[] };
}

export const useHeaderStatus = defineStore('headerStatus', () => {
  const instances = useInstances();
  const data = shallowRef<HeaderStatus>();
  const selection = ref('');
  const loading = ref(false),
    busy = ref(false),
    error = ref(''),
    message = ref('');
  const receivedAt = ref(0),
    now = ref(Date.now() / 1000),
    controlVersion = ref(0);
  let revision = 0,
    controller: AbortController | undefined;
  let lastSelected: string | null | undefined;
  const fresh = computed(
    () =>
      !!data.value &&
      !error.value &&
      !!data.value.online &&
      now.value - receivedAt.value <= 15 &&
      now.value - data.value.transfer_summary.time <= 15,
  );
  const chosen = computed(() => (data.value?.job?.id === selection.value ? data.value.job : null));
  const enabled = computed(
    () =>
      !!selection.value &&
      selection.value === data.value?.selected_job_id &&
      !data.value.paused &&
      !data.value.stop_requested,
  );
  const editable = bootstrap().role !== 'viewer';
  const canStart = computed(
    () =>
      editable &&
      fresh.value &&
      !busy.value &&
      !!chosen.value &&
      !chosen.value.archived &&
      !chosen.value.deleted &&
      !!(chosen.value.queued || chosen.value.running || chosen.value.importing) &&
      !enabled.value &&
      !(data.value?.stop_requested && data.value.running),
  );
  const canPause = computed(
    () => editable && fresh.value && !busy.value && !!data.value && !data.value.paused,
  );
  const canStop = computed(
    () =>
      editable &&
      fresh.value &&
      !busy.value &&
      !!data.value &&
      (!!data.value.running || !data.value.paused) &&
      !data.value.stop_requested,
  );
  const download = computed(() => {
    const sample = data.value?.transfer_summary;
    if (!fresh.value || !sample || (sample.transferring && !sample.measured)) return null;
    return sample.speed / 1e6;
  });
  const output = computed(() =>
    fresh.value && data.value?.session?.output_mbps != null
      ? data.value.session.output_mbps / 8
      : null,
  );
  const cooling = computed(() => {
    const sample = data.value?.account_summary;
    if (
      !fresh.value ||
      !sample ||
      (sample.accounts.length &&
        (!sample.sample_time ||
          now.value - sample.sample_time >= 20 ||
          sample.accounts.some((a) => !a.known)))
    )
      return null;
    const context = {
      fresh: true,
      now: now.value,
      pending: data.value?.queued || 0,
      paused: data.value?.paused || false,
    };
    return sample.accounts.filter((a) => !a.shared_with && accountState(a, context) === 'cooldown')
      .length;
  });
  const label = computed(() => {
    if (busy.value) return '处理中';
    if (error.value) return '读取失败';
    if (!data.value) return '读取中';
    if (!data.value.online) return '实例离线';
    if (!fresh.value) return '待更新';
    if (data.value.stop_requested && data.value.running) return '停止中';
    if (data.value.paused) return data.value.running ? '收尾中' : '已暂停';
    return enabled.value ? '已启动' : '待启动';
  });
  function clear() {
    revision++;
    controller?.abort();
    controller = undefined;
    data.value = undefined;
    receivedAt.value = 0;
    loading.value = false;
  }
  function adopt(value: HeaderStatus | WorkbenchStatus) {
    const header = { ...value } as HeaderStatus & Partial<WorkbenchStatus>;
    delete header.active_tasks;
    data.value = header;
    error.value = '';
    receivedAt.value = now.value = Date.now() / 1000;
  }
  async function refresh(signal?: AbortSignal, poll = false) {
    if (busy.value || (bootstrap().fleetEnabled && !instances.selectedId)) return;
    controller?.abort();
    const current = new AbortController(),
      ticket = ++revision,
      scope = instances.scope();
    const requested = selection.value;
    controller = current;
    const abort = () => current.abort();
    signal?.addEventListener('abort', abort, { once: true });
    if (signal?.aborted) abort();
    loading.value = true;
    try {
      const params = new URLSearchParams({ compact: '1' });
      if (requested) params.set('job_id', requested);
      if (poll) params.set('poll', '1');
      const result = await api.request<HeaderStatus>('/api/jobs/workbench?' + params, {
        scope,
        signal: current.signal,
        timeoutMs: 30000,
      });
      if (ticket !== revision || current.signal.aborted || !scope.isCurrent()) return;
      const changed = lastSelected !== undefined && result.selected_job_id !== lastSelected;
      lastSelected = result.selected_job_id;
      adopt(result);
      if (changed && result.selected_job_id !== requested) {
        selection.value = result.selected_job_id || '';
        clear();
        void refresh();
      } else if (!requested && result.selected_job_id) {
        // The response already describes the actual selected task.
        selection.value = result.selected_job_id;
      } else if (requested && !result.job) {
        selection.value = '';
        message.value = '所选任务已删除，请重新选择。';
      }
    } catch (failure) {
      if (ticket === revision && !current.signal.aborted && scope.isCurrent())
        error.value = failure instanceof Error ? failure.message : String(failure);
    } finally {
      signal?.removeEventListener('abort', abort);
      if (ticket === revision) loading.value = false;
      if (controller === current) controller = undefined;
    }
  }
  function choose(id: string) {
    if (busy.value || id === selection.value) return;
    selection.value = id;
    message.value = '';
    error.value = '';
    clear();
    void refresh();
  }
  async function invalidate() {
    controlVersion.value++;
    clear();
    await refresh();
  }
  async function control(action: 'start' | 'pause' | 'stop') {
    if (
      !(action === 'start' ? canStart.value : action === 'pause' ? canPause.value : canStop.value)
    )
      return;
    const scope = instances.scope();
    const jobId = selection.value;
    busy.value = true;
    error.value = '';
    message.value = '';
    clear();
    try {
      await api.request('/api/jobs/workbench', {
        method: 'POST',
        scope,
        body: { action, job_id: jobId },
      });
      if (scope.isCurrent())
        message.value = {
          start: '已启动所选任务。',
          pause: '已暂停领取新任务，当前下载完成后停止。',
          stop: '已请求立即停止当前实例。',
        }[action];
    } catch (failure) {
      if (scope.isCurrent())
        message.value = failure instanceof Error ? failure.message : String(failure);
    } finally {
      busy.value = false;
      await invalidate();
    }
  }
  watch(
    () => instances.epoch,
    () => {
      clear();
      selection.value = '';
      lastSelected = undefined;
      error.value = message.value = '';
      if (location.pathname !== '/workbench') void refresh();
    },
    { flush: 'sync' },
  );
  return {
    data,
    selection,
    loading,
    busy,
    error,
    message,
    now,
    fresh,
    chosen,
    enabled,
    canStart,
    canPause,
    canStop,
    download,
    output,
    cooling,
    label,
    controlVersion,
    refresh,
    choose,
    invalidate,
    adopt,
    control,
    clear,
  };
});
