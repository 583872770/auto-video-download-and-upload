export interface DiskPressure {
  kind: string;
  category?: string;
  requested_bytes: number;
  allocatable_bytes: number;
}
export interface DiskSource {
  key: string;
  label: string;
  used_bytes: number | null;
  status: string;
  same_filesystem: boolean | null;
  reclaimable_bytes?: number | null;
  retention_days?: number | null;
  note: string;
}
export interface DiskDiagnostic {
  sampled_at: number;
  severity: 'normal' | 'warning' | 'critical' | 'blocked';
  kind: string;
  headline: string;
  used_percent: number;
  total_bytes: number;
  free_bytes: number;
  minimum_free_bytes: number;
  resume_free_bytes: number;
  unwritten_reserved_bytes: number;
  allocatable_bytes: number;
  waiting_tasks: number;
  budget: {
    limit_bytes: number;
    committed_bytes: number;
    category_label: string;
    category_limit_bytes: number | null;
    category_committed_bytes: number | null;
  };
  pressure: Partial<DiskPressure>;
  retention: {
    status?: string;
    completed_count?: number;
    completed_bytes?: number;
    pending_count?: number;
    pending_bytes?: number;
    failed_count?: number;
    failure_reasons?: string[];
    failure_checks?: number;
    untracked_bytes?: number;
    untracked_overdue?: boolean;
  };
  host_status: string;
  host_sampled_at: number | null;
  sources: DiskSource[];
}
export function diskBytes(value?: number | null): string {
  if (value == null || !Number.isFinite(value)) return '待检测';
  if (value === 0) return '0 B';
  const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB'];
  const index = Math.min(4, Math.max(0, Math.floor(Math.log(value) / Math.log(1024))));
  return `${(value / 1024 ** index).toFixed(index ? 1 : 0)} ${units[index]}`;
}
export function diskLabel(disk?: DiskDiagnostic | null): string {
  if (!disk) return '磁盘待更新';
  const label = { normal: '磁盘', warning: '空间预警', critical: '空间紧张', blocked: '空间受阻' }[
    disk.severity
  ];
  return `${label} ${disk.used_percent.toFixed(1)}%`;
}
