import type { Page } from '../jobs/contracts';
export interface MediaItem {
  id: string;
  name: string;
  title: string;
  size: number;
  cover: string | null;
  playable: boolean;
  instance_id?: string | null;
}
export interface MediaPage extends Page<MediaItem> {
  directory: string;
  total: number;
  indexing: { scanned: number; last_indexed: number | null };
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
