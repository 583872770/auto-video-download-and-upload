import type { ConsoleConfig } from '../../generated/config';
export type TaskStorage = Pick<ConsoleConfig, 'path' | 'dir' | 'shard_downloads' | 'storage_mode' | 'oss_endpoint' | 'oss_bucket' | 'oss_prefix' | 'oss_access_key_id' | 'oss_access_key_secret' | 'oss_security_token' | 'oss_upload_limit' | 'oss_upload_threads' | 'oss_part_size_mb'>;
export function defaultStorage(): TaskStorage {
  return { path: 'videos', dir: '', shard_downloads: true, storage_mode: 'local', oss_endpoint: '', oss_bucket: '', oss_prefix: 'videos', oss_access_key_id: '', oss_access_key_secret: '', oss_security_token: '', oss_upload_limit: 2, oss_upload_threads: 2, oss_part_size_mb: 8 };
}
export type OutputKind = 'subtitle' | 'video_only' | 'audio_only' | 'av';
export interface DeliverySpec {
  schema_version: number;
  storage: TaskStorage & { oss_access_key_secret_configured?: boolean; oss_security_token_configured?: boolean };
  outputs: OutputKind[];
  video: { max_height: number; container: string };
  audio: { format: string };
  av: { container: string };
  subtitles: {
    mode: 'original' | 'original_and_translated';
    languages: string[];
    format: string;
    sources: string[];
    language_policy: string;
    missing_policy: string;
  };
}
export const outputLabels: Record<OutputKind, string> = {
  subtitle: '字幕',
  video_only: '无声视频',
  audio_only: '纯音频',
  av: '音视频成品',
};
export const deliveryStates: Record<string, string> = {
  pending: '待处理',
  partial: '部分交付',
  review: '待确认缺失',
  complete: '全部交付',
  accepted: '已按约定结束',
  done: '已交付',
  failed: '失败待补交',
  missing: '来源缺失',
  waived: '已接受缺失',
};
export function defaultDelivery(): DeliverySpec {
  return {
    schema_version: 1,
    storage: defaultStorage(),
    outputs: ['av'],
    video: { max_height: 1080, container: 'source' },
    audio: { format: 'source' },
    av: { container: 'auto' },
    subtitles: {
      mode: 'original',
      languages: ['zh-Hans'],
      format: 'srt',
      sources: ['manual', 'automatic'],
      language_policy: 'available_selected',
      missing_policy: 'review',
    },
  };
}
export function validateDelivery(spec: DeliverySpec): string {
  if (!spec.storage.path.trim()) return '请填写本任务的素材根目录。';
  if (!['local', 'oss', 'test'].includes(spec.storage.storage_mode)) return '请选择有效的文件保存方式。';
  if (!spec.outputs.length || spec.outputs.some((kind) => !(kind in outputLabels)))
    return '请至少选择一种有效交付内容。';
  const languages = spec.subtitles.languages;
  if (
    (!languages.length && spec.subtitles.mode !== 'original') ||
    languages.length > 8 ||
    languages.some((value) => !/^[A-Za-z][A-Za-z0-9-]{0,30}$/.test(value))
  )
    return '请填写 1～8 个有效字幕语言代码。';
  return '';
}
export interface DeliveryArtifact {
  id: string;
  seq?: number;
  job_id: string;
  video_id: string;
  title: string;
  kind: OutputKind;
  language: string;
  subtitle_source?: 'manual' | 'original_auto' | 'translated' | '';
  size: number;
  storage: string;
  available: boolean;
  content_url: string;
  instance_id: string;
  created_at?: string;
}
export interface DeliveryRequirement {
  key: string;
  task_id: string;
  kind: OutputKind;
  language: string;
  status: string;
  error: string;
  waived_reason: string;
}
export interface SourceDelivery {
  version?: number;
  status: string;
  test_mode?: boolean;
  requirements: DeliveryRequirement[];
}
export interface JobDelivery {
  contract: { version: number; spec: DeliverySpec; summary: string } | null;
  states: Record<string, number>;
  types: Partial<Record<OutputKind, { file_count: number; file_bytes: number }>>;
}
