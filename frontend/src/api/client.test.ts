import { afterEach, describe, expect, it, vi } from 'vitest';
import { ApiClient } from './client';
afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});
describe('same-origin API', () => {
  it('exports Cookie blobs with scope fencing and preserves JSON errors', async () => {
    const fetcher = vi
      .fn()
      .mockResolvedValueOnce(new Response('cookie fixture'))
      .mockResolvedValueOnce(new Response('{"error":"凭据已删除"}', { status: 409 }));
    vi.stubGlobal('fetch', fetcher);
    const client = new ApiClient(() => 'csrf');
    const blob = await client.request<Blob>('/api/youtube/cookies/export', {
      method: 'POST',
      body: { id: 'profile' },
      scope: { id: 'alpha', epoch: 0, isCurrent: () => true },
      responseType: 'blob',
    });
    expect(await blob.text()).toBe('cookie fixture');
    expect(new Headers(fetcher.mock.calls[0]![1].headers).get('X-Instance-ID')).toBe('alpha');
    await expect(
      client.request('/api/youtube/cookies/export', {
        method: 'POST',
        body: { id: 'missing' },
        responseType: 'blob',
      }),
    ).rejects.toMatchObject({ status: 409, message: '凭据已删除' });
  });
  it('keeps incorrect login credentials on the login page', async () => {
    const expired = vi.fn();
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(new Response('{"error":"用户名或密码不正确。"}', { status: 401 })),
    );
    await expect(
      new ApiClient(() => '', expired).request('/api/auth/login', {
        method: 'POST',
        body: { username: 'fixture', password: 'bad' },
      }),
    ).rejects.toMatchObject({ status: 401, message: '用户名或密码不正确。' });
    expect(expired).not.toHaveBeenCalled();
  });
  it('reads subtitle text while preserving authentication and structured errors', async () => {
    const fetcher = vi
      .fn()
      .mockResolvedValueOnce(new Response('WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nHello'))
      .mockResolvedValueOnce(new Response('{"error":"文件不存在"}', { status: 404 }));
    vi.stubGlobal('fetch', fetcher);
    const client = new ApiClient(() => 'csrf');
    expect(await client.request('/api/delivery/files/fixture', { responseType: 'text' })).toContain(
      'WEBVTT',
    );
    await expect(
      client.request('/api/delivery/files/missing', { responseType: 'text' }),
    ).rejects.toMatchObject({ status: 404, message: '文件不存在' });
  });
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
