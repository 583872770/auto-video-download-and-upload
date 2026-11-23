import { beforeEach, describe, expect, it, vi } from 'vitest';
import { createPinia, setActivePinia } from 'pinia';
import { defaultConfig } from '../generated/config';
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
    store.update('proxy', 'pending');
    await expect(store.load()).rejects.toThrow('请先保存或放弃');
    expect(store.draft.proxy).toBe('pending');
    expect(request).toHaveBeenCalledTimes(1);
  });
  it('serializes the task cap and accepts the server readback', async () => {
    request.mockResolvedValueOnce(response());
    const store = useSettings();
    await store.load();
    store.update('download_task_limit', 27);
    request
      .mockResolvedValueOnce({})
      .mockResolvedValueOnce(response({ ...defaultConfig, download_task_limit: 27 }));
    await store.save();
    expect(request.mock.calls[1]![1]?.body).toEqual({ download_task_limit: 27 });
    expect(store.error).toBe('');
    expect(store.dirty).toBe(false);
    expect(store.message).toContain('已保存');
  });
  it('keeps input when a committed write cannot be confirmed and never retries automatically', async () => {
    request.mockResolvedValueOnce(response());
    const store = useSettings();
    await store.load();
    store.update('proxy', 'pending');
    request.mockResolvedValueOnce({}).mockRejectedValueOnce(new Error('offline'));
    await store.save();
    expect(store.draft.proxy).toBe('pending');
    expect(store.dirty).toBe(true);
    expect(store.error).toContain('已提交');
    expect(request).toHaveBeenCalledTimes(3);
    store.reset();
    expect(store.dirty).toBe(false);
  });
  it('saves the reduction switch as a boolean and confirms its readback', async () => {
    request.mockResolvedValueOnce(response());
    const store = useSettings();
    await store.load();
    expect(store.draft.download_auto_reduce).toBe(true);
    store.update('download_auto_reduce', false);
    request
      .mockResolvedValueOnce({})
      .mockResolvedValueOnce(response({ ...defaultConfig, download_auto_reduce: false }));
    await store.save();
    expect(request.mock.calls[1]![1]?.body).toEqual({ download_auto_reduce: false });
    expect(store.draft.download_auto_reduce).toBe(false);
    expect(store.error).toBe('');
    expect(store.dirty).toBe(false);
  });
});
