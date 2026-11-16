import { beforeEach, describe, expect, it, vi } from 'vitest';
import { createPinia, setActivePinia } from 'pinia';
import { api } from '../api';
import { useInstances } from './instances';
import { useHeaderStatus, type HeaderStatus } from './headerStatus';
import type { InstanceInfo } from '../api/types';

vi.mock('../api', () => ({ api: { request: vi.fn() } }));
vi.mock('../bootstrap', () => ({ bootstrap: () => ({ fleetEnabled: true, role: 'admin' }) }));
const request = vi.mocked(api.request);
const time = Date.now() / 1000;
function snapshot(overrides: Partial<HeaderStatus> = {}): HeaderStatus {
  return {
    selected_job_id: 'job-a',
    paused: false,
    online: true,
    running: 1,
    queued: 3,
    job: { id: 'job-a', name: 'A', queued: 3, running: 1, archived: false, deleted: false },
    error: '',
    session: { output_mbps: 19.2 },
    transfer_summary: { time, transferring: 1, measured: 1, speed: 38600000 },
    account_summary: { sample_time: time, accounts: [] },
    ...overrides,
  } as HeaderStatus;
}
beforeEach(() => {
  setActivePinia(createPinia());
  vi.resetAllMocks();
  vi.stubGlobal('location', new URL('http://localhost/?instance_id=alpha'));
  vi.stubGlobal('localStorage', { getItem: vi.fn(), setItem: vi.fn() });
  vi.stubGlobal('history', { replaceState: vi.fn() });
  const instances = useInstances();
  instances.items = [
    { id: 'alpha', name: '洛杉矶' },
    { id: 'beta', name: '东京' },
  ] as InstanceInfo[];
  instances.select('alpha');
});
describe('shared header execution state', () => {
  it('shows selected-instance rates in MB/s and pauses via the existing workbench API', async () => {
    request.mockResolvedValueOnce(snapshot());
    const store = useHeaderStatus();
    await store.refresh();
    expect(store.enabled).toBe(true);
    expect(store.download).toBe(38.6);
    expect(store.output).toBe(2.4);
    request.mockResolvedValueOnce({}).mockResolvedValueOnce(snapshot({ paused: true }));
    await store.control('pause');
    expect(request).toHaveBeenCalledWith(
      '/api/jobs/workbench',
      expect.objectContaining({
        method: 'POST',
        body: { action: 'pause', job_id: 'job-a' },
        scope: expect.objectContaining({ id: 'alpha' }),
      }),
    );
    expect(store.enabled).toBe(false);
    expect(store.label).toBe('收尾中');
  });
  it('selecting a task only previews it; empty, offline and stale states cannot start', async () => {
    const store = useHeaderStatus();
    request.mockResolvedValue(snapshot({ selected_job_id: null, job: null, paused: true }));
    await store.refresh();
    await store.control('start');
    expect(store.canStart).toBe(false);
    request.mockResolvedValue(snapshot());
    await store.refresh();
    request.mockResolvedValue(
      snapshot({ selected_job_id: 'job-a', job: { ...snapshot().job!, id: 'job-b' } }),
    );
    store.choose('job-b');
    await store.refresh();
    expect(store.enabled).toBe(false);
    expect(store.canStart).toBe(true);
    expect(request.mock.calls.every(([, options]) => options?.method !== 'POST')).toBe(true);
    store.now = time + 16;
    expect(store.canStart).toBe(false);
    expect(store.download).toBeNull();
    request.mockResolvedValue(snapshot({ online: false }));
    await store.refresh();
    expect(store.canPause).toBe(false);
    expect(store.canStop).toBe(false);
    expect(store.output).toBeNull();
  });
  it('discards a late response from the previous instance', async () => {
    let old!: (value: HeaderStatus) => void;
    request.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          old = resolve;
        }),
    );
    const store = useHeaderStatus();
    const pending = store.refresh();
    request.mockResolvedValueOnce(
      snapshot({
        selected_job_id: null,
        job: null,
        paused: true,
        transfer_summary: { time, transferring: 0, measured: 0, speed: 0 },
      }),
    );
    useInstances().select('beta');
    await vi.waitFor(() => expect(store.download).toBe(0));
    old(snapshot());
    await pending;
    expect(store.selection).toBe('');
    expect(store.download).toBe(0);
    expect(store.enabled).toBe(false);
  });
  it('uses account-state rules for cooldowns, excludes aliases and expires old samples', async () => {
    const cooling = {
      id: 'cool',
      name: '',
      known: true,
      state: 'cooldown',
      tasks: [],
      reasons: [{ code: 'account_cooldown', label: '', until: time + 100 }],
    };
    request.mockResolvedValue(
      snapshot({
        account_summary: {
          sample_time: time,
          accounts: [
            cooling,
            { ...cooling, id: 'alias', shared_with: 'cool' },
            {
              ...cooling,
              id: 'busy',
              tasks: [{ task_id: 't', title: '', stage: 'downloading', transfer_reserved: true }],
            },
            { ...cooling, id: 'manual', state: 'manual' },
          ],
        },
      }),
    );
    const store = useHeaderStatus();
    await store.refresh();
    expect(store.cooling).toBe(1);
    store.now = time + 21;
    expect(store.cooling).toBeNull();
    request.mockRejectedValueOnce(new Error('读取超时'));
    await store.refresh();
    expect(store.label).toBe('读取失败');
    expect(store.download).toBeNull();
    expect(store.canPause).toBe(false);
  });
});
