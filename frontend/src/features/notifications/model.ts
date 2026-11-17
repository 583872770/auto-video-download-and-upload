export type NoticeState = 'action' | 'recovering' | 'resolved';
export type NoticeCounts = Record<NoticeState, number>;
export interface NoticeDetail {
  disk?: import('../disk/model').DiskDiagnostic;
  disk_pressure?: import('../disk/model').DiskPressure;
  recovery?: { freed_bytes: number; recovered_at: number; paused: boolean };
  code: string;
  task_id?: string | null;
  video_id?: string;
  title?: string;
  headline: string;
  phase: string;
  reason: string;
  raw?: string;
  next_step: string;
  completed: string[];
  missing: string[];
  retry_at?: number | null;
  attempts?: number;
  max_attempts?: number;
  http_status?: number | null;
  affected_tasks?: number;
  task_status?: string;
  prolonged?: boolean;
  activity?: string;
}
export interface Notification {
  id: string;
  instance_id: string;
  instance_name: string;
  job_id?: string | null;
  job_name: string;
  customer: string;
  code: string;
  status: NoticeState;
  active_count: number;
  first_seen: number;
  updated_at: number;
  resolved_at?: number | null;
  detail: NoticeDetail;
}
export interface NoticePage {
  resource_alerts?: Notification[];
  counts: NoticeCounts;
  global_counts: NoticeCounts;
  items: Notification[];
  next_cursor: string | null;
  sampled_at: number;
}
export interface AffectedTask {
  task_id: string;
  status: string;
  state: NoticeState;
  detail: NoticeDetail;
}
export function countdown(until: number | null | undefined, now: number): string {
  if (!until) return '';
  const remaining = Math.ceil(until - now);
  if (remaining <= 0) return '已到检查时间，等待执行条件';
  const hours = Math.floor(remaining / 3600);
  return (
    (hours ? `${hours}:` : '') +
    `${Math.floor((remaining % 3600) / 60)}`.padStart(2, '0') +
    ':' +
    `${remaining % 60}`.padStart(2, '0') +
    ' 后重新检查'
  );
}
