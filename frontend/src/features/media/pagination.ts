import type { UnifiedMediaPage } from './contracts';

export interface MediaPosition {
  media: string | null;
  artifacts: string | null;
  mediaExhausted: boolean;
  artifactsExhausted: boolean;
}

export function firstMediaPage(): MediaPosition {
  return { media: null, artifacts: null, mediaExhausted: false, artifactsExhausted: false };
}

export function nextMediaPage(current: MediaPosition, result: UnifiedMediaPage): MediaPosition {
  return {
    media: result.media.next_cursor,
    artifacts: result.artifacts.next_cursor,
    mediaExhausted: current.mediaExhausted || !result.media.next_cursor,
    artifactsExhausted: current.artifactsExhausted || !result.artifacts.next_cursor,
  };
}
