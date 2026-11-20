import { describe, expect, it } from 'vitest';
import { confirmation, type FailedTest } from './failedTests';
const item = { video_id: 'npNnbj46_ik', bytes: 3 * 1024 ** 2 } as FailedTest;
describe('failed test remedies', () => {
  it('confirms exact scope, data loss and network cost separately', () => {
    const redownload = confirmation(item, 'redownload', '洛杉矶');
    expect(redownload).toContain('洛杉矶');
    expect(redownload).toContain('npNnbj46_ik');
    expect(redownload).toContain('3.0 MiB');
    expect(redownload).toContain('账号和网络流量');
    expect(confirmation(item, 'retry', '洛杉矶')).toContain('保留现有源文件');
    expect(confirmation(item, 'discard', '洛杉矶')).toContain('不再自动重试');
    for (const action of ['retry', 'redownload', 'discard'] as const)
      expect(confirmation(item, action, '洛杉矶')).toContain('已登记统计保留');
  });
});
