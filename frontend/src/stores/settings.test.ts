import { beforeEach, describe, expect, it, vi } from 'vitest';
import { createPinia, setActivePinia } from 'pinia';
import { defaultConfig, type DownloadChannel } from '../generated/config';
import { api } from '../api';
import { useSettings } from './settings';

vi.mock('../api', () => ({ api: { request: vi.fn() } }));
vi.mock('./instances', () => ({
  useInstances: () => ({
    name: 'fixture',
    scope: () => ({ id: 'a', epoch: 1, isCurrent: () => true }),
  }),
}));
const request = vi.mocked(api.request);
const response = (config = defaultConfig) => ({
  config: structuredClone(config),
  database_budget: null,
});
beforeEach(() => {
  setActivePinia(createPinia());
  vi.resetAllMocks();
});

describe('instance settings drafts', () => {
  it('does not overwrite unsaved input when refreshed', async () => {
    request.mockResolvedValueOnce(response());
    const store = useSettings();
    await store.load();
    store.update('oss_bucket', 'pending');
    await expect(store.load()).rejects.toThrow('请先保存或放弃');
    expect(store.draft.oss_bucket).toBe('pending');
    expect(request).toHaveBeenCalledTimes(1);
  });
  it('serializes nested Vue channel proxies, then accepts the server readback', async () => {
    const channel: DownloadChannel = {
      id: 'test',
      label: 'test',
      adapter: 'bergung',
      site_url: 'https://site.example/',
      api_url: 'https://api.example/',
      upstream_group: 'g',
      resolver_group: 'g',
      quota_group: 'g',
      transfer_limit: 8,
      resolver_limit: 2,
      connection_limit: 16,
      task_limit: 4,
      weight: 1,
      verification: 'auto',
    };
    request.mockResolvedValueOnce(response());
    const store = useSettings();
    await store.load();
    store.update('download_channels', [channel]);
    request
      .mockResolvedValueOnce({})
      .mockResolvedValueOnce(response({ ...defaultConfig, download_channels: [channel] }));
    await store.save();
    expect(request.mock.calls[1]![1]?.body).toEqual({ download_channels: [channel] });
    expect(store.error).toBe('');
    expect(store.dirty).toBe(false);
    expect(store.message).toContain('已保存');
  });
  it('keeps input when a committed write cannot be confirmed and never retries automatically', async () => {
    request.mockResolvedValueOnce(response());
    const store = useSettings();
    await store.load();
    store.update('oss_prefix', 'pending');
    request.mockResolvedValueOnce({}).mockRejectedValueOnce(new Error('offline'));
    await store.save();
    expect(store.draft.oss_prefix).toBe('pending');
    expect(store.dirty).toBe(true);
    expect(store.error).toContain('已提交');
    expect(request).toHaveBeenCalledTimes(3);
    store.reset();
    expect(store.dirty).toBe(false);
  });
});
