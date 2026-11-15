import { afterEach, describe, expect, it, vi } from 'vitest';
import { ApiError } from '../../api/client';
import { appendLog, readTaskData } from './reading';

afterEach(() => vi.useRealTimers());

describe('independent log reads', () => {
  it('retries a temporary read failure once using the same cursor', async () => {
    vi.useFakeTimers();
    const cursor = 42;
    const read = vi
      .fn()
      .mockRejectedValueOnce(new ApiError('读取超时', 0))
      .mockResolvedValue({ text: '后续日志', offset: cursor + 12 });
    const result = readTaskData(() => read(cursor), new AbortController().signal);
    await vi.advanceTimersByTimeAsync(401);
    expect(await result).toEqual({ text: '后续日志', offset: 54 });
    expect(read.mock.calls).toEqual([[42], [42]]);
  });

  it('does not retry permission, missing-task or instance-switch errors', async () => {
    for (const error of [
      new ApiError('无权访问', 403),
      new ApiError('任务不存在', 404),
      new DOMException('实例切换', 'AbortError'),
    ]) {
      const read = vi.fn().mockRejectedValue(error);
      await expect(readTaskData(read, new AbortController().signal)).rejects.toBe(error);
      expect(read).toHaveBeenCalledOnce();
    }
  });

  it('cancels the pending retry when the dialog closes or task changes', async () => {
    vi.useFakeTimers();
    const controller = new AbortController();
    const read = vi.fn().mockRejectedValue(new TypeError('network failed'));
    const result = readTaskData(read, controller.signal);
    const assertion = expect(result).rejects.toMatchObject({ name: 'AbortError' });
    await vi.advanceTimersByTimeAsync(0);
    controller.abort();
    await vi.advanceTimersByTimeAsync(500);
    await assertion;
    expect(read).toHaveBeenCalledOnce();
  });

  it('stops after the second transient failure', async () => {
    vi.useFakeTimers();
    const read = vi.fn().mockRejectedValue(new ApiError('仍然超时', 0));
    const result = readTaskData(read, new AbortController().signal);
    const assertion = expect(result).rejects.toThrow('仍然超时');
    await vi.advanceTimersByTimeAsync(401);
    await assertion;
    expect(read).toHaveBeenCalledTimes(2);
  });

  it('reassembles partial lines and preserves text already loaded before retention', () => {
    const first = appendLog(undefined, { text: '[event] {"event":', offset: 17 });
    const second = appendLog(first, { text: '"attempt_start"}\n', offset: 34 });
    expect(second.text).toBe('[event] {"event":"attempt_start"}\n');
    const missing = appendLog(second, { text: '', offset: 0, message: '日志已清理' });
    expect(missing.text).toBe(second.text);
    expect(missing.message).toBe('日志已清理');
    expect(appendLog(second, { text: '新日志\n', offset: 99, pruned: true }).text).toBe(
      '[早期日志已自动清理]\n新日志\n',
    );
  });

  it('bounds stored text without exposing a partial first line', () => {
    const result = appendLog(undefined, {
      text: 'older\n' + 'x'.repeat(200005) + '\n最新日志\n',
      offset: 300000,
    });
    expect(result.text.length).toBeLessThanOrEqual(200000);
    expect(result.text).toBe('最新日志\n');
  });
});
