import { computed, ref } from 'vue';
import { defineStore } from 'pinia';
import { api } from '../api';
import type { ConfigResponse, DatabaseBudget } from '../api/types';
import {
  defaultConfig,
  type ConfigKey,
  type ConsoleConfig,
  type Provider,
} from '../generated/config';
import { useInstances } from './instances';
import { changes, unconfirmed } from '../features/settings/model';

export const useSettings = defineStore('settings', () => {
  const instances = useInstances();
  const baseline = ref<ConsoleConfig | null>(null);
  const draft = ref<ConsoleConfig>(structuredClone(defaultConfig));
  const budget = ref<DatabaseBudget | null>(null);
  const loading = ref(false),
    saving = ref(false),
    loaded = ref(false);
  const error = ref(''),
    message = ref('');
  const issues = ref<Partial<Record<ConfigKey, string>>>({});
  const patch = computed(() => changes(baseline.value, draft.value));
  const dirty = computed(() => Object.keys(patch.value).length > 0);
  const providers = computed<Provider[]>(
    () =>
      draft.value.download_methods ||
      (draft.value.download_method === 'yt5s_fallback'
        ? ['yt5s', 'yt_dlp']
        : [draft.value.download_method as Provider]),
  );

  function accept(response: ConfigResponse) {
    const config = { ...structuredClone(defaultConfig), ...response.config };
    baseline.value = structuredClone(config);
    draft.value = structuredClone(config);
    budget.value = response.database_budget;
    loaded.value = true;
    issues.value = {};
  }
  function update<K extends ConfigKey>(key: K, value: ConsoleConfig[K]) {
    draft.value[key] = value;
    delete issues.value[key];
    error.value = '';
    message.value = '';
  }
  function setProviders(value: Provider[]) {
    update('download_methods', value);
  }
  async function load(discard = false) {
    if (dirty.value && !discard) throw new Error('请先保存或放弃当前实例的修改。');
    const scope = instances.scope();
    loaded.value = false;
    loading.value = true;
    error.value = '';
    message.value = '';
    try {
      accept(await api.request<ConfigResponse>('/api/config', { scope, cache: 'no-store' }));
    } catch (failure) {
      if (scope.isCurrent())
        error.value = failure instanceof Error ? failure.message : '读取设置失败。';
    } finally {
      if (scope.isCurrent()) loading.value = false;
    }
  }
  async function save() {
    if (!loaded.value || saving.value || !dirty.value) return;
    const scope = instances.scope();
    const submitted = JSON.parse(JSON.stringify(patch.value)) as Partial<ConsoleConfig>;
    saving.value = true;
    error.value = '';
    let committed = false;
    try {
      await api.request('/api/config', { method: 'PUT', body: submitted, scope });
      committed = true;
      const response = await api.request<ConfigResponse>('/api/config', {
        scope,
        cache: 'no-store',
      });
      const mismatches = unconfirmed(submitted, response.config);
      if (mismatches.length)
        throw new Error('服务器读回值与提交值不一致：' + mismatches.join('、'));
      accept(response);
      message.value = `「${instances.name}」系统设置已保存。后续领取或重试的视频使用新配置。`;
    } catch (failure) {
      if (scope.isCurrent())
        error.value =
          (committed ? '已提交，保存结果尚未确认：' : '保存失败：') +
          (failure instanceof Error ? failure.message : '请求失败') +
          '；修改已保留。';
    } finally {
      saving.value = false;
    }
  }
  function reset() {
    if (baseline.value) draft.value = JSON.parse(JSON.stringify(baseline.value));
    issues.value = {};
    error.value = '';
    message.value = '';
  }
  const changedCount = computed(() => Object.keys(patch.value).length);
  return {
    draft,
    baseline,
    budget,
    loaded,
    loading,
    saving,
    error,
    message,
    issues,
    dirty,
    changedCount,
    providers,
    update,
    setProviders,
    load,
    save,
    reset,
  };
});
