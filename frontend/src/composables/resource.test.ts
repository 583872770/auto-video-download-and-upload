import { describe, expect, it } from 'vitest';
import { useResource } from './resource';

describe('useResource live snapshot state', () => {
  it('records the last successful snapshot and resets failures', async () => {
    const resource = useResource<{ value: number }>();
    expect(await resource.load(async () => ({ value: 1 }))).toBe(true);
    expect(resource.data.value).toEqual({ value: 1 });
    expect(resource.lastSuccessAt.value).toBeGreaterThan(0);
    expect(resource.consecutiveFailures.value).toBe(0);
  });

  it('keeps the last good snapshot while reporting a failed poll', async () => {
    const resource = useResource<{ value: number }>();
    await resource.load(async () => ({ value: 1 }));
    const lastSuccess = resource.lastSuccessAt.value;
    expect(
      await resource.load(async () => {
        throw new Error('读取超时');
      }),
    ).toBe(false);
    expect(resource.data.value).toEqual({ value: 1 });
    expect(resource.lastSuccessAt.value).toBe(lastSuccess);
    expect(resource.consecutiveFailures.value).toBe(1);
    expect(resource.error.value).toBe('读取超时');
  });

  it('does not let a superseded request overwrite the newer result', async () => {
    const resource = useResource<{ value: number }>();
    let resolveOld!: (value: { value: number }) => void;
    const old = resource.load(
      () =>
        new Promise((resolve) => {
          resolveOld = resolve;
        }),
    );
    const current = resource.load(async () => ({ value: 2 }));
    expect(await current).toBe(true);
    resolveOld({ value: 1 });
    expect(await old).toBeUndefined();
    expect(resource.data.value).toEqual({ value: 2 });
  });
});
