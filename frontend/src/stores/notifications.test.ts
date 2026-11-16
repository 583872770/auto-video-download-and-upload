import { beforeEach, describe, expect, it, vi } from 'vitest';
import { createPinia, setActivePinia } from 'pinia';
import { api } from '../api';
import { useNotifications } from './notifications';
import { countdown, type NoticePage } from '../features/notifications/model';

vi.mock('../api', () => ({ api: { request: vi.fn() } }));
const request = vi.mocked(api.request);
function page(overrides: Partial<NoticePage> = {}): NoticePage {
  return {
    counts: { action: 1, recovering: 2, resolved: 3 },
    global_counts: { action: 4, recovering: 5, resolved: 6 },
    items: [],
    next_cursor: null,
    sampled_at: 1000,
    ...overrides,
  };
}
beforeEach(() => {
  setActivePinia(createPinia());
  vi.resetAllMocks();
});
describe('persistent global notifications', () => {
  it('uses a compact global request without the selected-instance header', async () => {
    request.mockResolvedValue(page());
    const store = useNotifications();
    await store.refresh();
    expect(request).toHaveBeenCalledWith(
      '/api/notifications?state=action&compact=1',
      expect.not.objectContaining({ scope: expect.anything() }),
    );
    expect(store.globalCounts.action).toBe(4);
    store.open = true;
    store.instance = 'alpha';
    store.job = 'job-a';
    await store.refresh();
    expect(store.counts.action).toBe(1);
    expect(store.globalCounts.action).toBe(4);
    expect(request.mock.calls[1][0]).toContain('instance_id=alpha');
    expect(request.mock.calls[1][0]).toContain('job_id=job-a');
  });
  it('reading or closing a notification never acknowledges an unresolved problem', async () => {
    request.mockResolvedValue(page());
    const store = useNotifications();
    store.show();
    await vi.waitFor(() => expect(store.globalCounts.action).toBe(4));
    store.close();
    expect(store.globalCounts.action).toBe(4);
    expect(
      request.mock.calls.every(([, options]) => !options?.method || options.method === 'GET'),
    ).toBe(true);
  });
  it('keeps known problems when polling fails instead of replacing them with zero', async () => {
    const store = useNotifications();
    request.mockResolvedValueOnce(page()).mockRejectedValueOnce(new Error('网络读取超时'));
    await store.refresh();
    await store.refresh();
    expect(store.globalCounts.action).toBe(4);
    expect(store.error).toBe('网络读取超时');
  });
  it('discards a late response from the previous task filter', async () => {
    let complete!: (value: NoticePage) => void;
    request.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          complete = resolve;
        }),
    );
    const store = useNotifications();
    store.open = true;
    store.instance = 'alpha';
    const old = store.refresh();
    store.instance = 'beta';
    request.mockResolvedValueOnce(page({ counts: { action: 0, recovering: 0, resolved: 0 } }));
    await store.refresh();
    complete(page());
    await old;
    expect(store.counts.action).toBe(0);
    expect(store.globalCounts.action).toBe(4);
  });
  it('shows eligibility as a check time rather than guaranteeing download starts', () => {
    expect(countdown(1061, 1000)).toBe('01:01 后重新检查');
    expect(countdown(1000, 1000)).toBe('已到检查时间，等待执行条件');
    expect(countdown(4661, 1000)).toBe('1:01:01 后重新检查');
    expect(countdown(null, 1000)).toBe('');
  });
  it('preserves appended pages while polling only the summary', async () => {
    const first = { id: 'first' } as NoticePage['items'][number];
    const second = { id: 'second' } as NoticePage['items'][number];
    request
      .mockResolvedValueOnce(page({ items: [first], next_cursor: 'next' }))
      .mockResolvedValueOnce(page({ items: [second] }))
      .mockResolvedValueOnce(page());
    const store = useNotifications();
    store.open = true;
    await store.refresh();
    await store.refresh(undefined, true);
    await store.refresh();
    expect(store.items.map((item) => item.id)).toEqual(['first', 'second']);
    expect(request.mock.calls[2][0]).toContain('compact=1');
  });
});
