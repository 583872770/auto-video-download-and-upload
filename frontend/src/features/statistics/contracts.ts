export interface Totals {
  file_bytes_exact: string;
  test_file_bytes_exact: string;
  downloaded_file_bytes_exact: string;
  file_count: number;
  test_file_count: number;
  downloaded_file_count: number;
  succeeded: number;
  failed: number;
  skipped: number;
  test_succeeded: number;
  running: number;
  attempted: number;
  unfinished: number;
}
export interface Method extends Totals {
  method: string;
  label: string;
  success_rate: number | null;
  fallback_tasks: number;
}
export interface Session extends Totals {
  id: string;
  seq: number;
  label: string;
  status: string;
  started_at: string;
  ended_at?: string;
}
export interface Ledger extends Totals {
  id: string;
  name: string;
  customer: string;
  created_at: string;
  deleted: boolean;
  archived: boolean;
  tracked: boolean;
  amount: string | null;
  price_set: boolean;
  price: string;
  unit: string;
}
export interface Entry {
  task_id: string;
  video_id: string;
  title: string;
  path: string;
  size_exact: string;
  size: number;
  created_at: string;
  finished_at?: string;
  started_at?: string;
  status: string;
  error?: string;
}
export const integer = (value: string | number | undefined) =>
  BigInt(String(value || 0)).toLocaleString('zh-CN');
export function gb(value: string | number | undefined) {
  const bytes = BigInt(String(value || 0));
  return `${(bytes / 1000000000n).toLocaleString('zh-CN')}.${((bytes % 1000000000n) / 1000n).toString().padStart(6, '0')} GB`;
}
export const time = (value?: string) => (value ? new Date(value).toLocaleString('zh-CN') : '—');
