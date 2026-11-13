import { computed, ref } from 'vue';
import { defineStore } from 'pinia';
import { api } from '../api';
import type { RequestScope } from '../api/client';
import type { FleetResponse, InstanceInfo } from '../api/types';
import { bootstrap } from '../bootstrap';

const storageKey = () => 'frameflow-instance:' + location.origin;

export const useInstances = defineStore('instances', () => {
  const items = ref<InstanceInfo[]>([]);
  const selectedId = ref('');
  const epoch = ref(0);
  const loading = ref(false);
  const error = ref('');
  const current = computed(() => items.value.find((item) => item.id === selectedId.value));
  const name = computed(
    () => current.value?.name || (bootstrap().fleetEnabled ? '未选择实例' : '当前实例'),
  );

  function scope(): RequestScope {
    const id = selectedId.value,
      version = epoch.value;
    return {
      id,
      epoch: version,
      isCurrent: () => selectedId.value === id && epoch.value === version,
    };
  }
  function select(id: string) {
    if (id && !items.value.some((item) => item.id === id)) throw new Error('请选择已登记的实例。');
    selectedId.value = id;
    epoch.value++;
    try {
      localStorage.setItem(storageKey(), id);
    } catch {
      // Restricted browser storage must not prevent managing an instance.
    }
    const url = new URL(location.href);
    if (id) url.searchParams.set('instance_id', id);
    else url.searchParams.delete('instance_id');
    history.replaceState(null, '', url);
  }
  async function load() {
    if (!bootstrap().fleetEnabled) return;
    loading.value = true;
    error.value = '';
    try {
      const response = await api.request<FleetResponse>('/api/instances?compact=1');
      items.value = response.items.filter((item) => ['worker', 'hybrid'].includes(item.role));
      let remembered = '';
      try {
        remembered = localStorage.getItem(storageKey()) || '';
      } catch {
        // The URL and server default remain available without browser storage.
      }
      const preferred =
        new URLSearchParams(location.search).get('instance_id') ||
        remembered ||
        response.instance_id;
      const item =
        items.value.find((item) => item.id === preferred) ||
        items.value.find((item) => item.online) ||
        items.value[0];
      if (!item) throw new Error('还没有可管理的实例，请检查服务注册。');
      select(item.id);
    } catch (failure) {
      error.value = failure instanceof Error ? failure.message : '读取实例失败。';
    } finally {
      loading.value = false;
    }
  }
  return { items, selectedId, epoch, loading, error, current, name, scope, select, load };
});
