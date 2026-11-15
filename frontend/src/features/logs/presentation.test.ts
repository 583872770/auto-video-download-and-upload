import { describe, expect, it } from 'vitest';
import { formatLogBytes, readableLog } from './presentation';

const time = '2026-11-15T10:55:35.392+08:00';
const line = (text: string) => `${time} ${text}`;
const event = (name: string, fields: Record<string, unknown> = {}) =>
  line('[event] ' + JSON.stringify({ event: name, ...fields }));

describe('readable execution logs', () => {
  it('explains the supplied subtitle task with one final byte summary', () => {
    const text = [
      line('[download] 任务开始'),
      event('attempt_start', { attempt: 1 }),
      event('attempt_released', {
        reason: 'pipeline_backpressure',
        message: 'Cookie 账号池正在按账号并发及请求间隔等待。',
        retry_after: 2,
      }),
      line('[download] 任务开始'),
      event('attempt_start', { attempt: 1 }),
      event('cookie_pool_selected', { profile_id: 'technical-id' }),
      event('execution_policy', { storage_mode: 'local' }),
      event('connection_selected', { proxy_mode: 'direct' }),
      event('provider_start', { provider: 'yt_dlp' }),
      line('[youtube] Downloading webpage'),
      line('[youtube] Sleeping 0.75 seconds ...'),
      event('subtitle_selected', { language: 'en', source: 'original_auto' }),
      line('[info] Downloading 1 format(s): 248'),
      line('[download] Sleeping 10.88 seconds ...'),
      line(
        'WARNING: The extractor specified to use impersonation for this download, but no impersonate target is available.',
      ),
      line('[download] Destination: /internal/workspace/caption.en.vtt'),
      ...[9.1, 27.2, 63.4, 100].map((value) =>
        line(`[download] ${value}% of 11.04KiB at Unknown B/s ETA Unknown`),
      ),
      event('local_processing_end', { operation: 'delivery_subtitle', outcome: 'completed' }),
      event('subtitle_processed', { language: 'en', source_format: 'vtt', target_format: 'srt' }),
      line('yt-dlp 完成：传输 11.0 KB，生成文件 6.1 KB。'),
      event('provider_end', { outcome: 'completed', transferred_bytes: 11305, output_bytes: 6248 }),
      line('任务执行完成。传输数据：11.0 KB；文件大小：6.1 KB。'),
      event('attempt_end', {
        outcome: 'succeeded',
        file_count: 1,
        file_bytes: 6248,
        elapsed_seconds: 19.586,
      }),
    ].join('\n');
    const entries = readableLog(text);
    const visible = entries.map((entry) => entry.text).join('\n');
    expect(visible).toContain('2 秒后重新检查');
    expect(visible).toContain('不计入失败重试');
    expect(visible).toContain('视频原语言自动识别字幕');
    expect(visible).not.toContain('自动翻译字幕');
    expect(visible).toContain('10.9 秒');
    expect(visible).toContain('VTT → SRT');
    expect(visible.match(/11.0 KB/g)).toHaveLength(1);
    expect(visible.match(/6.1 KB/g)).toHaveLength(1);
    expect(visible).not.toMatch(/technical-id|Downloading|workspace|\[event\]|248/);
    expect(entries.filter((entry) => entry.progress)).toHaveLength(1);
    expect(entries.at(-1)?.level).toBe('success');
    expect(visible).toContain('本次执行耗时 19.6 秒');
    expect(entries.find((entry) => entry.text.includes('浏览器请求模拟'))?.level).toBe('warning');
  });

  it('keeps failures and a scheduled retry visible without declaring success', () => {
    const result = readableLog(
      [
        event('attempt_start', { attempt: 2 }),
        event('provider_end', {
          outcome: 'failed',
          failure: { message: 'Unable to download subtitles: HTTP Error 429: Too Many Requests' },
        }),
        event('attempt_end', {
          outcome: 'queued',
          retry_scheduled: true,
          retry_delay_seconds: 60,
          failure: { message: 'HTTP Error 429: Too Many Requests' },
        }),
      ].join('\n'),
    );
    expect(result.at(-1)?.text).toContain('已安排重试');
    expect(result.at(-1)?.text).toContain('HTTP 429');
    expect(result.at(-1)?.text).toContain('60 秒后重新检查');
    expect(result.some((entry) => entry.level === 'success')).toBe(false);
  });

  it('distinguishes translated and manual tracks from source-language recognition', () => {
    const result = readableLog(
      [
        event('subtitle_selected', { language: 'zh-Hans', source: 'translated' }),
        event('subtitle_selected', { language: 'ja', source: 'manual' }),
      ].join('\n'),
    );
    expect(result[0].text).toContain('自动翻译字幕');
    expect(result[1].text).toContain('作者提供的人工字幕');
  });

  it('does not reuse transfer bytes from an earlier attempt', () => {
    const result = readableLog(
      [
        event('attempt_start'),
        event('provider_end', { transferred_bytes: 5000, outcome: 'failed' }),
        event('attempt_start'),
        event('attempt_end', { outcome: 'succeeded', skipped: true, elapsed_seconds: 1 }),
      ].join('\n'),
    );
    expect(result.at(-1)?.text).toContain('跳过重复下载');
    expect(result.at(-1)?.text).not.toContain('下载数据');
  });

  it('retains readable errors and handles an incomplete event without throwing', () => {
    const result = readableLog(
      line('磁盘资源等待：本机空间不足。') + '\n' + line('[event] {"event":"attempt_'),
    );
    expect(result).toHaveLength(1);
    expect(result[0].text).toContain('空间不足');
    expect(
      readableLog(event('unknown', { outcome: 'failed', message: '字幕转换失败。' }))[0].text,
    ).toContain('字幕转换失败');
  });

  it('uses the same binary byte convention as task completion messages', () => {
    expect(formatLogBytes(11305)).toBe('11.0 KB');
    expect(formatLogBytes(6248)).toBe('6.1 KB');
    expect(formatLogBytes(0)).toBe('0 B');
    expect(formatLogBytes(-1)).toBe('未知');
  });
});
