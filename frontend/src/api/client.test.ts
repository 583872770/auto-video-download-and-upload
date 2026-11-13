import { afterEach, describe, expect, it, vi } from 'vitest';
import { ApiClient } from './client';
afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});
describe('same-origin API', () => {
  it('adds CSRF and selected instance to mutations, never retries a write', async () => {
    const fetcher = vi
      .fn()
      .mockResolvedValue(new Response('{"error":"conflict"}', { status: 409 }));
    vi.stubGlobal('fetch', fetcher);
    await expect(
      new ApiClient(() => 'csrf').request('/api/config', {
        method: 'PUT',
        body: { worker_count: 2 },
        scope: { id: 'worker', epoch: 1, isCurrent: () => true },
      }),
    ).rejects.toThrow('conflict');
    expect(fetcher).toHaveBeenCalledTimes(1);
    const init = fetcher.mock.calls[0]![1] as RequestInit;
    expect(new Headers(init.headers).get('X-CSRF-Token')).toBe('csrf');
    expect(new Headers(init.headers).get('X-Instance-ID')).toBe('worker');
    expect(init.credentials).toBe('same-origin');
  });
  it('keeps global requests independent and redirects expired sessions', async () => {
    const expired = vi.fn(),
      fetcher = vi.fn().mockResolvedValue(new Response('{}', { status: 401 }));
    vi.stubGlobal('fetch', fetcher);
    await expect(
      new ApiClient(() => '', expired).request('/api/delivery/defaults'),
    ).rejects.toMatchObject({ status: 401 });
    expect(expired).toHaveBeenCalledOnce();
    expect(new Headers(fetcher.mock.calls[0]![1].headers).has('X-Instance-ID')).toBe(false);
  });
  it('discards responses from a previous instance', async () => {
    let current = true;
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => {
        current = false;
        return new Response('{}');
      }),
    );
    await expect(
      new ApiClient(() => '').request('/api/config', {
        scope: { id: 'old', epoch: 1, isCurrent: () => current },
      }),
    ).rejects.toMatchObject({ name: 'AbortError' });
  });
  it('aborts timeouts and reports uncertain write results', async () => {
    vi.useFakeTimers();
    const fetcher = vi.fn(
      (_path, init: RequestInit) =>
        new Promise((_resolve, reject) =>
          init.signal!.addEventListener('abort', () =>
            reject(new DOMException('aborted', 'AbortError')),
          ),
        ),
    );
    vi.stubGlobal('fetch', fetcher);
    const result = new ApiClient(() => '').request('/api/config', {
      method: 'PUT',
      body: {},
      timeoutMs: 10,
    });
    const assertion = expect(result).rejects.toThrow('结果尚未确认');
    await vi.advanceTimersByTimeAsync(11);
    await assertion;
    expect(fetcher).toHaveBeenCalledOnce();
  });
});
