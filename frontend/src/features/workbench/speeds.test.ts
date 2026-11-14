import { describe, expect, it } from 'vitest';
import type { RuntimeTask } from './contracts';
import { downloadSpeed, speedNumber } from './speeds';

const now = 1000;
function task(progress: RuntimeTask['progress'], extra: Partial<RuntimeTask> = {}): RuntimeTask {
  return { status: 'running', progress, ...extra } as RuntimeTask;
}
describe('workbench download speed', () => {
  it('adds transfers across accounts while ignoring waiting and completed task rates', () => {
    const tasks = [
      task({ stage: 'downloading', speed: 8e6, _reported_at: now }),
      task({ stage: 'downloading', speed: 12e6, _reported_at: now }),
      task({ stage: 'waiting_request', speed: 90e6, _reported_at: now }),
      task({ stage: 'downloading', speed: 90e6, _reported_at: now }, { status: 'succeeded' }),
    ];
    expect(downloadSpeed(tasks, now)).toEqual({
      transferring: 2,
      measured: 2,
      bytesPerSecond: 20e6,
    });
  });
  it('expires reported and stalled byte samples, and uses the current cache timestamp', () => {
    const tasks = [
      task({ stage: 'downloading', speed: 1e6, _reported_at: now - 11 }),
      task({ stage: 'downloading', speed: 2e6, _reported_at: now, last_bytes_at: now - 11 }),
      task(
        { stage: 'downloading', speed: 3e6, _reported_at: now - 20 },
        { progress_updated_at: now },
      ),
    ];
    expect(downloadSpeed(tasks, now)).toEqual({
      transferring: 3,
      measured: 1,
      bytesPerSecond: 3e6,
    });
    expect(downloadSpeed(tasks, now + 11).bytesPerSecond).toBeNull();
  });
  it('distinguishes zero transfers from unavailable measurements', () => {
    expect(downloadSpeed([], now).bytesPerSecond).toBe(0);
    for (const speed of [undefined, NaN, Infinity, -1]) {
      expect(
        downloadSpeed([task({ stage: 'downloading', speed, _reported_at: now })], now)
          .bytesPerSecond,
      ).toBeNull();
    }
    expect(
      downloadSpeed([task({ stage: 'downloading', speed: 123 })], now).bytesPerSecond,
    ).toBeNull();
    expect(speedNumber(null)).toBe('—');
    expect(speedNumber(0)).toBe('0.00');
  });
});
