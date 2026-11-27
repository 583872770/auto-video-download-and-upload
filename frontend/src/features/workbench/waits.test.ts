import { describe, expect, it } from 'vitest';
import { sampleNow, waitCountdown } from './waits';

describe('task wait countdown', () => {
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
