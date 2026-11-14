import type { Page } from '../jobs/contracts';
import type { DeliveryArtifact } from '../delivery/model';
export interface MediaItem {
  id: string;
  name: string;
  title: string;
  size: number;
  downloaded_at?: number | null;
  video_id?: string | null;
  cover: string | null;
  playable: boolean;
  instance_id?: string | null;
}
export interface MediaPage extends Page<MediaItem> {
  directory: string;
  total: number;
  indexing: { scanned: number; last_indexed: number | null };
}
export interface UnifiedMediaPage {
  media: MediaPage;
  artifacts: Page<DeliveryArtifact> & { total: number; counts: Record<string, number> };
  counts: Record<'all' | 'video' | 'audio' | 'subtitle', number>;
  directory: string;
  indexing: MediaPage['indexing'];
}
export function mediaAddress(kind: 'videos' | 'covers', name: string, instance?: string | null) {
  return (
    '/media/' +
    kind +
    '/' +
    name.split('/').map(encodeURIComponent).join('/') +
    (instance ? '?instance_id=' + encodeURIComponent(instance) : '')
  );
}
