import { describe, expect, it } from 'vitest';
import { diskBytes, diskLabel } from './model';

describe('disk readings', () => {
  it('distinguishes missing samples from measured zero and uses binary units', () => {
    expect(diskBytes(null)).toBe('待检测');
    expect(diskBytes(undefined)).toBe('待检测');
    expect(diskBytes(0)).toBe('0 B');
    expect(diskBytes(1024 ** 3)).toBe('1.0 GiB');
    expect(diskLabel(null)).toBe('磁盘待更新');
  });
});
