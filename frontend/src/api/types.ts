import type { ConsoleConfig } from '../generated/config';

export interface InstanceInfo {
  id: string;
  name: string;
  role: string;
  online: boolean;
  running: number;
  paused: number;
  stop_requested: number;
  error: string;
}
export interface FleetResponse {
  instance_id: string;
  items: InstanceInfo[];
}
export interface DatabaseBudget {
  mode: string;
  total: number;
  workers: number;
  required_total: number;
  target_workers: number;
  per_worker: number;
  main: number[];
  auxiliary: number;
  deployment_cap: number | null;
  deployment_worker_cap: number | null;
}
export interface ConfigResponse {
  config: ConsoleConfig;
  database_budget: DatabaseBudget;
  config_path: string;
  media_directory: string;
  instance_id?: string;
}
export interface DiskSnapshot {
  free_bytes: number;
  used_bytes: number;
  budget_bytes: number;
  low_space: boolean;
  active: Record<string, number>;
  waiting_tasks: number;
  message: string;
}
export interface RuntimeSummary {
  scope: string;
  since: number;
  until: number;
  coverage: string;
  totals: {
    completed: number;
    succeeded: number;
    test_succeeded: number;
    failed: number;
    skipped: number;
    success_rate: number | null;
    file_count: number;
    file_bytes: number;
  };
  failure_reasons: Record<string, number>;
  hours: Array<{
    hour: number;
    succeeded: number;
    failed: number;
    skipped: number;
    file_bytes: number;
  }>;
  log_retention: null | {
    sampled_at: number;
    budget_bytes: number;
    retained_task_files: number;
    database_available: boolean;
    unexpired_failure_files_removed: number;
  };
}
export interface InstanceStatus {
  database: string;
  tools: Record<string, string | boolean | null>;
  throughput: Record<string, unknown>;
  concurrency: Record<string, unknown> & {
    running: number;
    transferring: number;
    preparing: number;
    processing: number;
    effective_limit: number;
    connection_used: number;
    connection_budget: number;
    bottleneck: string;
    risks: Array<{ code: string; severity: string; detail: string }>;
  };
}
