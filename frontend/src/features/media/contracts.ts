import type { Page } from '../jobs/contracts';
import type { DeliveryArtifact } from '../delivery/model';
import { outputLabels, type OutputKind } from '../delivery/model';
export type LibraryKind = OutputKind | 'source_video';
export const libraryTypeLabels: Record<LibraryKind, string> = {
  ...outputLabels,
  source_video: '本地视频',
};
export interface MediaGroup {
  group_key: string;
  title: string;
  video_id: string | null;
  file_count: number;
  size: number;
  downloaded_at: number | null;
  kind_counts: Partial<Record<LibraryKind, number>>;
}
export interface MediaGroupsPage {
  items: MediaGroup[];
  total: number;
  next_cursor: string | null;
  counts: Record<'all' | LibraryKind, number>;
  file_count: number;
  subtitle_files: number;
  size: number;
  directory: string;
}
export interface LibraryFile {
  id: string;
  kind: LibraryKind;
  size: number;
  downloaded_at?: number | string | null;
  local?: MediaItem;
  artifact?: DeliveryArtifact;
}
export interface GroupFilesPage extends Page<LibraryFile> {
  total: number;
}
export function formatFileSize(bytes: number) {
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  const unit = bytes > 0 ? Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), 4) : 0;
  return `${(bytes / 1024 ** unit).toLocaleString('zh-CN', { maximumFractionDigits: unit ? 1 : 0 })} ${units[unit]}`;
}
export function formatDownloadTime(value?: number | string | null) {
  if (!value) return '—';
  const date = new Date(typeof value === 'number' ? value * 1000 : value);
  return Number.isNaN(date.getTime()) ? '—' : date.toLocaleString('zh-CN');
}
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
