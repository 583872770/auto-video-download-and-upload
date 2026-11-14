import type { RuntimeSlot } from '../workbench/contracts';
export const base = '/api/account-library/youtube';
export interface Node {
  id: string;
  name: string;
  online: boolean;
}
export interface Cookie {
  id: string;
  name: string;
  instance_id: string;
  instance_name: string;
  online: boolean;
  pooled: boolean;
  active: boolean;
  account_id?: string;
  created_at: string;
  download_state: string;
  auth: {
    state: string;
    message?: string;
    checked_at?: number;
    last_valid_at?: number;
    connection?: string;
  };
  session?: RuntimeSlot;
}
export interface Account {
  id: string;
  email: string;
  note: string;
  instance_id: string | null;
  instance_name: string;
  online: boolean;
  version: number;
  cookies: Cookie[];
  cookie_count: number;
  auth_state: string;
  download_state: string;
}
export interface Overview {
  items: Account[];
  total: number;
  page: number;
  size: number;
  instances: Node[];
  local_instance_id: string;
}
export interface CookieEvent {
  occurred_at: number;
  event_type: string;
  message: string;
  kind?: string;
  profile_name?: string;
  previous_interval?: number;
  new_interval?: number;
  cooldown_until?: number;
  http_status?: number;
}
export const authLabels: Record<string, string> = {
  unlinked: '尚未登录',
  unchecked: '待检测',
  valid: '最近验证通过',
  expired: '检测已过期',
  not_logged_in: '需要重新登录',
  invalid: 'Cookie 无效 / 过期',
  verification_required: '需要网页验证',
  network_error: '暂时无法确认（网络）',
  rate_limited: '暂时无法确认（限流）',
  checking: '检测进行中',
  unconfirmed: '暂时无法确认',
  missing: '凭据缺失',
};
export const downloadLabels: Record<string, string> = {
  inactive: '未参与下载',
  ready: '正常',
  cooldown: '账号冷却中',
  recovery: '低频恢复',
  manual: '需人工处理',
  unavailable: 'Cookie 不可用',
  unknown: '状态待更新',
};
export const at = (stamp?: number) =>
  stamp ? new Date(stamp * 1000).toLocaleString('zh-CN') : '—';
