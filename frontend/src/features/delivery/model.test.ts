import { describe, expect, it } from 'vitest';
import { defaultDelivery, validateDelivery } from './model';
import { formatGB } from '../jobs/contracts';

describe('delivery editing contracts', () => {
  it('rejects empty output selection and malformed languages, including hidden subtitle fields', () => {
    const spec = defaultDelivery();
    spec.outputs = [];
    expect(validateDelivery(spec)).toContain('至少选择');
    spec.outputs = ['av'];
    spec.subtitles.languages = ['en', 'invalid language'];
    expect(validateDelivery(spec)).toContain('语言代码');
    spec.subtitles.languages = ['en', 'zh-Hans'];
    expect(validateDelivery(spec)).toBe('');
    expect(defaultDelivery().subtitles.mode).toBe('original');
    expect(defaultDelivery().subtitles.languages).toEqual(['zh-Hans']);
    spec.subtitles.languages = [];
    expect(validateDelivery(spec)).toBe('');
    spec.subtitles.mode = 'original_and_translated';
    expect(validateDelivery(spec)).toContain('语言代码');
  });
  it('formats exact byte strings beyond JavaScript safe integer without losing digits', () => {
    expect(formatGB('9007199254740993')).toBe('9,007,199.254740 GB');
    expect(formatGB('1000')).toBe('0.000001 GB');
    expect(formatGB('0')).toBe('0.000000 GB');
  });
  it('allows image and information delivery without media outputs', () => {
    const spec = defaultDelivery();
    spec.outputs = ['thumbnail', 'metadata'];
    expect(validateDelivery(spec)).toBe('');
  });
  it('keeps language selection independent from source priority and translation targets', () => {
    const spec = defaultDelivery();
    expect(spec.subtitles.scope).toBe('all_original');
    expect(spec.thumbnail.strategy).toBe('best');
    spec.subtitles.scope = 'selected';
    expect(validateDelivery(spec)).toContain('原始字幕语言代码');
    spec.subtitles.original_languages = ['en', 'ja'];
    expect(validateDelivery(spec)).toBe('');
    expect(spec.subtitles.sources).toEqual(['manual', 'automatic']);
    expect(spec.subtitles.languages).toEqual(['zh-Hans']);
  });
});
