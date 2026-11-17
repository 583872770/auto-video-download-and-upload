import { describe, expect, it } from 'vitest';
import { defaultConfig } from '../../generated/config';
import { changes, unconfirmed, validate } from './model';
describe('settings contracts', () => {
  it('preserves blank secrets, false and zero while submitting only changes', () => {
    const baseline = structuredClone(defaultConfig);
    const draft = {
      ...baseline,
      youtube_sleep_preset: !baseline.youtube_sleep_preset,
      download_task_limit: 0,
    };
    baseline.download_task_limit = 4;
    expect(changes(baseline, draft)).toEqual({
      download_task_limit: 0,
      youtube_sleep_preset: draft.youtube_sleep_preset,
    });
  });
  it('detects values not confirmed by the server', () => {
    expect(
      unconfirmed({ download_task_limit: 2 }, { ...defaultConfig, download_task_limit: 3 }),
    ).toEqual(['download_task_limit']);
  });
  it('validates hidden fields and cross-field budgets without rejecting unconfigured OSS', () => {
    expect(validate(defaultConfig)).toEqual([]);
    const invalid = {
      ...defaultConfig,
      download_task_limit: NaN,
      disk_resume_free_gb: 1,
      disk_min_free_gb: 2,
    };
    expect(validate(invalid).map((issue) => issue.key)).toEqual(
      expect.arrayContaining(['download_task_limit', 'disk_resume_free_gb']),
    );
  });
});
