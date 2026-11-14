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
    expect(defaultDelivery().subtitles.languages).toEqual(['en', 'zh-Hans', 'zh-Hant']);
  });
  it('formats exact byte strings beyond JavaScript safe integer without losing digits', () => {
    expect(formatGB('9007199254740993')).toBe('9,007,199.254740 GB');
    expect(formatGB('1000')).toBe('0.000001 GB');
    expect(formatGB('0')).toBe('0.000000 GB');
  });
});
