import { describe, expect, it } from 'vitest';
import { defaultConfig, type DownloadChannel } from '../../generated/config';
import { canonical, changes, unconfirmed, validate } from './model';
describe('settings contracts', () => {
  it('preserves blank secrets, false and zero while submitting only changes', () => {
    const baseline = structuredClone(defaultConfig);
    const draft = {
      ...baseline,
      youtube_sleep_preset: !baseline.youtube_sleep_preset,
      youtube_cookie_pool_worker_limit: 0,
    };
    baseline.youtube_cookie_pool_worker_limit = 4;
    expect(changes(baseline, draft)).toEqual({
      youtube_cookie_pool_worker_limit: 0,
      youtube_sleep_preset: draft.youtube_sleep_preset,
    });
  });
  it('accepts normalized endpoints and channel field order, detects unsaved fields', () => {
    const channel: DownloadChannel = {
      id: 'test',
      label: 'test',
      adapter: 'bergung',
      site_url: 'https://site.example',
      api_url: 'https://api.example',
      upstream_group: 'group',
      resolver_group: 'group',
      quota_group: 'group',
      transfer_limit: 8,
      resolver_limit: 2,
      connection_limit: 16,
      task_limit: 4,
      weight: 1,
      verification: 'auto',
    };
    const reordered = Object.fromEntries(Object.entries(channel).reverse());
    expect(canonical('download_channels', [channel])).toBe(
      canonical('download_channels', [reordered]),
    );
    expect(unconfirmed({ worker_count: 2 }, { ...defaultConfig, worker_count: 3 })).toEqual([
      'worker_count',
    ]);
  });
  it('validates hidden fields and cross-field budgets without rejecting unconfigured OSS', () => {
    expect(validate(defaultConfig)).toEqual([]);
    const invalid = {
      ...defaultConfig,
      worker_count: NaN,
      disk_resume_free_gb: 1,
      disk_min_free_gb: 2,
      download_methods: [],
    };
    expect(validate(invalid).map((issue) => issue.key)).toEqual(
      expect.arrayContaining(['worker_count', 'disk_resume_free_gb', 'download_methods']),
    );
  });
});
