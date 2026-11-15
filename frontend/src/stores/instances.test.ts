import { beforeEach, describe, expect, it, vi } from 'vitest';
import { createPinia, setActivePinia } from 'pinia';
import { api } from '../api';
import { useInstances } from './instances';

vi.mock('../api', () => ({ api: { request: vi.fn() } }));
vi.mock('../bootstrap', () => ({ bootstrap: () => ({ fleetEnabled: true }) }));
const request = vi.mocked(api.request);
const response = (name: string) => ({
  instance_id: 'alpha',
  items: [
    { id: 'alpha', name, role: 'hybrid', online: true },
    { id: 'beta', name: '东京', role: 'hybrid', online: true },
  ],
});

beforeEach(() => {
  setActivePinia(createPinia());
  vi.resetAllMocks();
  vi.stubGlobal('location', new URL('http://localhost/?instance_id=alpha'));
  vi.stubGlobal('localStorage', { getItem: vi.fn(), setItem: vi.fn() });
  vi.stubGlobal('history', { replaceState: vi.fn() });
});

describe('instance display name synchronization', () => {
  it('refreshes the selected name without invalidating requests or drafts', async () => {
    request.mockResolvedValueOnce(response('旧名称'));
    const store = useInstances();
    await store.load();
    const scope = store.scope();
    request.mockResolvedValueOnce(response('洛杉矶'));
    await store.load();
    expect(store.name).toBe('洛杉矶');
    expect(store.selectedId).toBe('alpha');
    expect(store.epoch).toBe(scope.epoch);
    expect(scope.isCurrent()).toBe(true);
  });

  it('propagates confirmed names and invalidates scope only on an actual switch', async () => {
    request.mockResolvedValueOnce(response('旧名称'));
    const store = useInstances();
    await store.load();
    const scope = store.scope();
    store.updateNames([
      { id: 'alpha', name: '洛杉矶' },
      { id: 'missing', name: '未知' },
    ]);
    expect(store.name).toBe('洛杉矶');
    expect(store.items).toHaveLength(2);
    expect(scope.isCurrent()).toBe(true);
    store.select('alpha');
    expect(scope.isCurrent()).toBe(true);
    store.select('beta');
    expect(scope.isCurrent()).toBe(false);
    expect(store.name).toBe('东京');
  });
});
