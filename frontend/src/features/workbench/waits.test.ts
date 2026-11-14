import { describe, expect, it } from 'vitest';
import type { AccountTask } from './contracts';
import { sampleNow, summarizeWaits, taskWait, taskWaitGroups, waitCountdown } from './waits';

const task = (overrides: Partial<AccountTask>): AccountTask => ({
  task_id: 'fixture',
  title: '视频',
  stage: 'waiting_transfer',
  transfer_reserved: false,
  ...overrides,
});

describe('task wait explanations', () => {
  it('counts tasks across accounts by actual resource rather than the shared waiting stage', () => {
    const local = { code: 'local', label: '本机传输名额暂不可用' };
    const shared = { code: 'shared', label: '共享下载连接暂不可用' };
    const result = summarizeWaits([
      task({ wait: local }),
      task({ task_id: 'second', wait: local }),
      task({ wait: shared }),
      task({ stage: 'downloading', wait: local }),
    ]);
    expect(result.map(({ label, count }) => ({ label, count }))).toEqual([
      { label: local.label, count: 2 },
      { label: shared.label, count: 1 },
    ]);
  });

  it('preserves reported detail and deadline while supporting older status responses', () => {
    const wait = {
      code: 'request',
      label: '视频启动请求间隔',
      detail: '等待请求时间',
      until: 1234,
    };
    expect(taskWait(task({ stage: 'waiting_request', wait }))).toEqual(wait);
    expect(taskWait(task({ stage: 'waiting_cookie' }))?.label).toBe('等待账号');
    expect(taskWait(task({ stage: 'extracting', wait }))).toBeUndefined();
  });

  it('keeps different deadlines visible instead of counting them under the first timer', () => {
    const wait = { code: 'interval', label: '请求间隔', until: 110 };
    const grouped = taskWaitGroups([
      task({ wait }),
      task({ wait }),
      task({ wait: { ...wait, until: 120 } }),
      task({ stage: 'downloading', wait }),
    ]);
    expect(grouped.map(({ until, count }) => ({ until, count }))).toEqual([
      { until: 110, count: 2 },
      { until: 120, count: 1 },
    ]);
  });

  it('advances from the server sample using elapsed time independently of the device clock', () => {
    const now = sampleNow(100, 8000, 10500);
    expect(now).toBe(102.5);
    expect(waitCountdown(110, now, true)).toBe('00:08 后检查');
    expect(waitCountdown(110, sampleNow(100, 8000, 11500), true)).toBe('00:07 后检查');
    expect(waitCountdown(110, sampleNow(106, 11500, 11500), true)).toBe('00:04 后检查');
  });

  it('formats long waits and expiry without negative times or claiming execution will start', () => {
    expect(waitCountdown(100 + 3661, 100, true)).toBe('01:01:01 后检查');
    expect(waitCountdown(100 + 90061, 100, true)).toBe('1天 01:01:01 后检查');
    expect(waitCountdown(100, 100, true)).toBe('已到期，等待状态更新');
    expect(waitCountdown(100, 110, true)).toBe('已到期，等待状态更新');
  });

  it('does not invent timers for resource waits or retain live timers for stale samples', () => {
    for (const until of [undefined, NaN, Infinity, 0]) {
      expect(waitCountdown(until, 100, true)).toBe('结束时间未定');
    }
    expect(waitCountdown(120, 100, false)).toBe('等待新鲜采样');
    expect(waitCountdown(120, 0, true)).toBe('等待新鲜采样');
  });
});
