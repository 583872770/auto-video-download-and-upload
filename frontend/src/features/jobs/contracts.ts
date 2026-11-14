import type { JobDelivery, SourceDelivery } from '../delivery/model';
export interface Page<T> {
  items: T[];
  next_cursor: string | null;
}
export interface VideoTask {
  id: string;
  video_id: string;
  title: string;
  url: string;
  status: string;
  instance_id: string;
  error: string;
  progress?: { stage?: string; percent?: number; speed?: number; title?: string };
  result?: { test_mode?: boolean; skipped?: boolean };
  delivery: SourceDelivery;
}
export interface Job {
  id: string;
  customer: string;
  name: string;
  note: string;
  price: string;
  unit: string;
  amount: string | null;
  price_set: boolean;
  state: string;
  total: number;
  queued: number;
  running: number;
  succeeded: number;
  failed: number;
  completed: number;
  removed: number;
  progress_percent: number | null;
  file_count: number;
  file_bytes_exact: string;
  test_succeeded: number;
  test_file_bytes_exact: string;
  skipped: number;
  archived: boolean;
  edit_version: number;
  waiting_reason: string;
  transfer_speed: number;
  delivery: JobDelivery;
  running_videos: VideoTask[];
  imports: Array<{
    id: string;
    name: string;
    finished: number;
    added: number;
    duplicates: number;
    invalid: number;
    samples: string;
  }>;
}
export interface JobSummary {
  total: number;
  running: number;
  file_bytes_exact: string;
  amount: string;
  unset: number;
}
export interface JobStatistics {
  events: Array<{ seq: number; message: string; created_at: string }>;
  providers: Array<{ method: string; status: string; count: number }>;
  files: Array<{ video_id: string; title: string; size: number }>;
  test_files: Array<{ video_id: string; title: string; size: number }>;
}
export const jobStates: Record<string, string> = {
  empty: '空任务',
  paused: '待开始 / 已暂停',
  waiting: '等待执行条件',
  queued: '排队中',
  running: '执行中',
  draining: '暂停中',
  importing: '导入中',
  completed: '已完成',
  partial: '完成但有失败',
  archived: '已归档',
  deleted: '已删除',
};
export const videoStates: Record<string, string> = {
  queued: '待处理',
  running: '执行中',
  succeeded: '处理成功',
  failed: '失败',
  interrupted: '已中断',
  removed: '已移除',
};
export function jobEndpoint(id: string) {
  return '/api/jobs/' + encodeURIComponent(id);
}
export function formatGB(bytes: number | string | undefined) {
  const value = BigInt(bytes || 0),
    whole = value / 1000000000n;
  const fraction = ((value % 1000000000n) / 1000n).toString().padStart(6, '0');
  return `${whole.toLocaleString('zh-CN')}.${fraction} GB`;
}
export function amount(job: Pick<Job, 'amount'>) {
  return job.amount === null ? '待设置单价' : `¥ ${job.amount}`;
}
