export interface RequestScope {
  id: string;
  epoch: number;
  isCurrent(): boolean;
}

export class ApiError extends Error {
  constructor(
    message: string,
    public readonly status: number,
  ) {
    super(message);
    this.name = 'ApiError';
  }
}

interface RequestOptions extends Omit<RequestInit, 'body'> {
  body?: unknown;
  scope?: RequestScope;
  timeoutMs?: number;
}

export class ApiClient {
  constructor(
    private readonly csrf: () => string,
    private readonly expired: () => void = () => location.assign('/login'),
  ) {}

  async request<T>(path: string, options: RequestOptions = {}): Promise<T> {
    const { body, scope, timeoutMs, signal, ...init } = options;
    const method = (init.method || 'GET').toUpperCase();
    const writing = !['GET', 'HEAD', 'OPTIONS'].includes(method);
    const headers = new Headers(init.headers);
    if (writing) headers.set('X-CSRF-Token', this.csrf());
    if (scope) headers.set('X-Instance-ID', scope.id);
    const form = body instanceof FormData;
    if (body !== undefined && !form) headers.set('Content-Type', 'application/json');
    const controller = new AbortController();
    const abort = () => controller.abort(signal?.reason);
    if (signal?.aborted) abort();
    else signal?.addEventListener('abort', abort, { once: true });
    let timedOut = false;
    const timer = setTimeout(
      () => {
        timedOut = true;
        controller.abort();
      },
      timeoutMs ?? (writing ? 90000 : 15000),
    );
    const current = () => {
      if (scope && !scope.isCurrent())
        throw new DOMException('实例已切换，已忽略旧请求结果。', 'AbortError');
    };
    try {
      current();
      const response = await fetch(path, {
        ...init,
        method,
        headers,
        credentials: 'same-origin',
        body: body === undefined ? undefined : form ? body : JSON.stringify(body),
        signal: controller.signal,
      });
      current();
      if (response.status === 401) {
        this.expired();
        throw new ApiError('登录已过期，请重新登录。', 401);
      }
      const raw = await response.text();
      current();
      let data: unknown;
      try {
        data = raw ? JSON.parse(raw) : {};
      } catch {
        throw new ApiError(`服务返回了非 JSON 响应（HTTP ${response.status}）。`, response.status);
      }
      if (!response.ok) {
        const message =
          typeof data === 'object' && data !== null && 'error' in data
            ? String(data.error)
            : `请求失败（HTTP ${response.status}）。`;
        throw new ApiError(message, response.status);
      }
      return data as T;
    } catch (error) {
      if (timedOut)
        throw new ApiError(
          writing ? '操作超时，结果尚未确认，请重新读取后核对。' : '读取超时，请稍后重试。',
          0,
        );
      throw error;
    } finally {
      clearTimeout(timer);
      signal?.removeEventListener('abort', abort);
    }
  }
}
