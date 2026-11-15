import { ApiError } from '../../api/client';

export interface LogResponse {
  text: string;
  offset: number;
  message?: string;
  pruned?: boolean;
}

export interface LogState {
  text: string;
  offset: number;
  message?: string;
}

function pause(signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    const abort = () => {
      clearTimeout(timer);
      signal.removeEventListener('abort', abort);
      reject(new DOMException('读取已取消。', 'AbortError'));
    };
    const timer = setTimeout(() => {
      signal.removeEventListener('abort', abort);
      resolve();
    }, 400);
    signal.addEventListener('abort', abort, { once: true });
    if (signal.aborted) abort();
  });
}

/** Retry a transient GET once, keeping the same task and log cursor. */
export async function readTaskData<T>(read: () => Promise<T>, signal: AbortSignal): Promise<T> {
  signal.throwIfAborted();
  try {
    return await read();
  } catch (error) {
    if (
      signal.aborted ||
      !(error instanceof TypeError || (error instanceof ApiError && error.status === 0))
    )
      throw error;
    await pause(signal);
    signal.throwIfAborted();
    return read();
  }
}

export function appendLog(previous: LogState | undefined, response: LogResponse): LogState {
  const text = (response.pruned ? '[早期日志已自动清理]\n' : previous?.text || '') + response.text;
  let bounded = text.slice(-200000);
  if (text.length > bounded.length) {
    const newline = bounded.indexOf('\n');
    if (newline >= 0) bounded = bounded.slice(newline + 1);
  }
  return { text: bounded, offset: response.offset, message: response.message };
}
