import type { Job, VideoTask } from '../jobs/contracts';
import type { InstanceStatus } from '../../api/types';
export interface RuntimeTask extends VideoTask {
  notification?: import('../notifications/model').NoticeDetail | null;
  action: string;
  created_at: string;
  started_at?: string;
  finished_at?: string;
  progress_updated_at?: number;
  progress?: VideoTask['progress'] & {
    _reported_at?: number;
    last_bytes_at?: number;
  };
}
export interface WorkbenchStatus {
  transfer_summary: {
    time: number;
    transferring: number;
    measured: number;
    speed: number;
  };
  disk?: import('../disk/model').DiskDiagnostic | null;
  disk_waiting?: DiskWaiting | null;
  hls_buffer?: HlsBufferSample | null;
  selected_job_id: string | null;
  job: (Job & { importing?: number }) | null;
  online: boolean;
  paused: boolean;
  stop_requested?: boolean;
  cleaning_temporary?: boolean;
  running: number;
  queued: number;
  active_tasks: RuntimeTask[];
  error: string;
  session: null | {
    file_bytes_exact: string;
    file_count: number;
    test_file_bytes_exact: string;
    downloaded_file_bytes_exact: string;
    elapsed_seconds: number;
    started_at: string;
    output_mbps: number | null;
  };
}

export interface DeliverySpeed {
  start_at: number;
  end_at: number;
  interval_seconds: number;
  points: Array<{
    start_at: number;
    end_at: number;
    file_bytes_exact: string;
    file_count: number;
    mbps: number;
    partial: boolean;
  }>;
}

export interface HlsBufferSample {
  sample_time: number;
  used_bytes: number;
  limit_bytes: number;
  per_file_bytes: number;
  occupied_slots: number;
  total_slots: number;
}
export interface AccountTask {
  task_id: string;
  title: string;
  stage: string;
  speed?: number;
  transfer_reserved: boolean;
  wait?: WaitReason | null;
}
export interface WaitReason {
  code: string;
  label: string;
  detail?: string;
  until?: number;
}
export interface ProcessRow {
  slot: string;
  number: number;
  processing: boolean;
  state: 'downloading' | 'pending_transfer' | 'pending_start' | 'disk_wait' | 'idle';
  stage: string;
  id?: string | null;
  video_id?: string | null;
  title?: string | null;
  account?: string | null;
  output?: string | null;
  language?: string | null;
  progress: VideoTask['progress'];
  wait?: { code?: string; label: string; detail?: string; until?: number } | null;
  phase_started_at?: number | null;
}
export type QueueGroup = 'processing' | 'retry' | 'ready';
export interface DiskWaiting {
  download: number;
  processing: number;
}
export interface QueuePage {
  items: { id: string; video_id?: string; title?: string; reason?: string; until?: number }[];
  next_cursor: number | null;
}
export interface LiveWorkbench {
  time: number;
  paused: boolean;
  rows: ProcessRow[];
  counts: Record<ProcessRow['state'], number>;
  disk_waiting: DiskWaiting;
  queue: Record<QueueGroup, number>;
  transfer_summary: WorkbenchStatus['transfer_summary'];
}
export interface Pacing {
  phase: string;
  recovery: boolean;
  target_reached: boolean;
  cooling_until: number;
  interval: number;
  configured_minimum: number;
  next_interval: number;
  observed_seconds: number;
  observation_seconds: number;
  success_count: number;
  success_target: number;
}
export interface RuntimeSlot {
  id: string;
  name: string;
  state: string;
  known: boolean;
  configured_limit: number;
  active: number;
  limit: number;
  interval: number;
  cooldown_until: number;
  pacing?: Pacing;
}
export type RuntimeAccount = Partial<RuntimeSlot> & {
  id: string;
  name: string;
  email?: string;
  shared_with?: string | null;
  tasks: AccountTask[];
  reasons?: WaitReason[];
};
export interface WorkerControl {
  auto_reduce_enabled: boolean;
  used: number;
  dynamic_limit: number;
  normal_limit: number;
  configured_limit: number;
  resource_limited: boolean;
  measurements?: {
    cpu_percent?: number;
    memory_available_mb?: number;
    io_pressure?: number;
    io_pressure_source?: string;
  };
  last_reduction?: {
    from: number;
    to: number;
    reasons: Array<{ code: string; value: number; threshold: number }>;
  };
  recovery?: {
    eligible_at: number;
    blocked_by: string[];
    cpu_below: number;
    io_below: number;
    memory_at_least_mb: number;
    interval_seconds: number;
    step: number;
  } | null;
}
export interface Capacity extends InstanceStatus {
  concurrency: InstanceStatus['concurrency'] & {
    time: number;
    pending_tasks: number;
    paused: boolean;
    sample_time?: number;
    online: boolean;
    mode: string;
    worker_control?: WorkerControl;
    disk_message: string;
    global_limit: number;
    global_running: number;
    local_processing?: { queued: number; running: number; limit: number };
    transfer_limit: number;
    requested: number;
    cookie_pool?: { sessions?: RuntimeSlot[] };
    cookie_allocation?: {
      accounts: RuntimeAccount[];
      unassigned_tasks: AccountTask[];
    };
    channels: Array<{
      id: string;
      label: string;
      occupied: number;
      effective_limit: number;
      status: string;
      cooldown_seconds: number;
      resources: Array<{
        scope: string;
        group: string;
        used: number;
        effective: number;
        phase: string;
        cooldown_seconds: number;
      }>;
    }>;
    decisions?: {
      combination: string;
      restrictions: Array<{
        scope: string;
        action: string;
        until: number;
        reason: string;
        manual: boolean;
      }>;
      recovery: { instructions: string };
      history: Array<{
        incident_id: string;
        failure?: { kind: string; message?: string; observed_at?: number };
        effects: Array<{ scope: string; action: string; until?: number }>;
      }>;
    };
  };
}
export const stageLabels: Record<string, string> = {
  downloading: '下载中',
  extracting: '解析视频',
  processing: '合并 / 后处理',
  cleaning: '清理文件',
  uploading_oss: '上传 OSS',
  preparing: '准备中',
  waiting_cookie: '等待账号',
  waiting_request: '等待请求间隔',
  waiting_connection: '连接 / 首字节等待',
  waiting_transfer: '等待来源服务 / 资源',
  waiting_processing: '等待后处理',
  disk_wait: '等待磁盘空间',
  checking_environment: '检查环境',
  checking_login: '检查登录',
  preparing_workspace: '准备工作目录',
  preparing_proxy: '准备代理',
  scheduling_wait: '等待调度',
  retrying: '等待重试',
  converting: '转换中',
  completed: '已完成',
};
export function stageLabel(stage?: string) {
  return stage ? stageLabels[stage] || '准备 / 其他阶段' : '等待状态更新';
}
