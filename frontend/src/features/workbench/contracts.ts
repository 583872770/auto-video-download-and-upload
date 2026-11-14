import type { Job, VideoTask } from '../jobs/contracts';
import type { InstanceStatus } from '../../api/types';
export interface RuntimeTask extends VideoTask {
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
  selected_job_id: string | null;
  job: (Job & { importing?: number }) | null;
  online: boolean;
  paused: boolean;
  stop_requested?: boolean;
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
    output_recent_mbps: number | null;
    output_history: {
      points: Array<{
        start_at: number;
        end_at: number;
        file_bytes: number;
        file_count: number;
        output_mbps: number | null;
        partial: boolean;
      }>;
    };
  };
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
  tasks: AccountTask[];
  reasons?: WaitReason[];
};
export interface WorkerControl {
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
  };
}
export interface Capacity extends InstanceStatus {
  concurrency: InstanceStatus['concurrency'] & {
    time: number;
    sample_time?: number;
    online: boolean;
    mode: string;
    worker_control?: WorkerControl;
    disk_message: string;
    global_limit: number;
    global_running: number;
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
  waiting_transfer: '等待传输名额',
  waiting_resolver: '等待解析名额',
  waiting_browser: '等待浏览器',
  waiting_verification: '等待网页验证',
  waiting_processing: '等待后处理',
  disk_wait: '等待磁盘空间',
  checking_environment: '检查环境',
  checking_login: '检查登录',
  switching_provider: '切换渠道',
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
