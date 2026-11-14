import { describe, expect, it } from 'vitest';
import type { UnifiedMediaPage } from './contracts';
import { firstMediaPage, nextMediaPage } from './pagination';

function response(media: string | null, artifacts: string | null): UnifiedMediaPage {
  return {
    media: { next_cursor: media },
    artifacts: { next_cursor: artifacts },
  } as UnifiedMediaPage;
}

describe('mixed material pagination', () => {
  it('keeps an exhausted local stream finished across subsequent output pages', () => {
    const first = firstMediaPage();
    const second = nextMediaPage(first, response(null, '50'));
    expect(second).toEqual({
      media: null,
      artifacts: '50',
      mediaExhausted: true,
      artifactsExhausted: false,
    });
    const third = nextMediaPage(second, response(null, '100'));
    expect(third.mediaExhausted).toBe(true);
    expect(third.artifacts).toBe('100');
    expect(first).toEqual(firstMediaPage());
  });

  it('keeps an exhausted output stream finished while local video pages continue', () => {
    const second = nextMediaPage(firstMediaPage(), response('24', null));
    const third = nextMediaPage(second, response('48', null));
    expect(third).toEqual({
      media: '48',
      artifacts: null,
      mediaExhausted: false,
      artifactsExhausted: true,
    });
    const end = nextMediaPage(third, response(null, null));
    expect(end.mediaExhausted && end.artifactsExhausted).toBe(true);
  });
});
