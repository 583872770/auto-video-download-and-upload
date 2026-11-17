import type { AccountTask, RuntimeAccount, WaitReason } from './contracts';
import { isWaitingStage } from './waits';

export const accountStates = [
  { key: 'running', label: '执行中' },
  { key: 'waiting', label: '等待中' },
  { key: 'cooldown', label: '冷却中' },
  { key: 'idle', label: '空闲' },
  { key: 'attention', label: '需处理' },
  { key: 'unknown', label: '状态待确认' },
] as const;
export type AccountState = (typeof accountStates)[number]['key'];
export interface AccountContext {
  fresh: boolean;
  now: number;
  pending: number;
  paused: boolean;
}
const terminal = new Set(['completed', 'failed', 'interrupted', 'cancelled']);
const manual = new Set(['account_halted', 'unavailable']);
const cooling = new Set(['account_cooldown']);
const taskGroups = [
  { key: 'preparing', label: '准备', tone: 'gray' },
  { key: 'extracting', label: '解析', tone: 'purple' },
  { key: 'downloading', label: '下载', tone: 'blue' },
  { key: 'processing', label: '处理', tone: 'teal' },
  { key: 'uploading', label: '上传', tone: 'teal' },
  { key: 'waiting', label: '等待', tone: 'amber' },
];

export function taskGroup(task: AccountTask) {
  if (terminal.has(task.stage)) return '';
  // A resource wait never counts as work already taking place.
  if (isWaitingStage(task.stage)) return 'waiting';
  if (task.stage === 'downloading') return 'downloading';
  if (['uploading', 'uploading_oss'].includes(task.stage)) return 'uploading';
  if (['processing', 'converting', 'cleaning'].includes(task.stage)) return 'processing';
  if (task.stage === 'extracting') return 'extracting';
  return 'preparing';
}

export function taskSummary(account: RuntimeAccount) {
  return taskGroups
    .map((group) => ({
      ...group,
      count: account.tasks.filter((task) => taskGroup(task) === group.key).length,
    }))
    .filter((group) => group.count);
}

export function accountReasons(account: RuntimeAccount, context: AccountContext): WaitReason[] {
  const hasWork = context.pending > 0 || account.tasks.some((task) => !!taskGroup(task));
  return (account.reasons || []).filter((reason) => {
    if (['offline', 'unknown', 'paused', 'unassigned'].includes(reason.code)) return false;
    if (reason.until && reason.until <= context.now) {
      // Keep expired cooldowns visible until a new sample confirms recovery.
      return cooling.has(reason.code);
    }
    return (
      hasWork ||
      manual.has(reason.code) ||
      cooling.has(reason.code) ||
      ['account_refresh', 'shared', 'removed'].includes(reason.code)
    );
  });
}

export function accountState(account: RuntimeAccount, context: AccountContext): AccountState {
  if (!context.fresh || !account.known || !account.state || account.state === 'unknown')
    return 'unknown';
  if (
    account.tasks.some((task) => {
      const group = taskGroup(task);
      return group && group !== 'waiting';
    })
  )
    return 'running';
  const reasons = accountReasons(account, context);
  if (['manual', 'unavailable'].includes(account.state) || reasons.some((r) => manual.has(r.code)))
    return 'attention';
  if (
    reasons.some((r) => cooling.has(r.code) && !!r.until && r.until > context.now) ||
    (account.state === 'cooldown' && (account.cooldown_until || 0) > context.now)
  )
    return 'cooldown';
  if (account.state === 'cooldown' || reasons.some((r) => r.code === 'account_refresh'))
    return 'unknown';
  if (context.pending > 0 || account.tasks.some((task) => taskGroup(task) === 'waiting'))
    return 'waiting';
  return 'idle';
}

export function accountSpeed(account: RuntimeAccount, fresh: boolean) {
  if (!fresh) return undefined;
  const rates = account.tasks
    .filter((task) => task.stage === 'downloading')
    .map((task) => task.speed)
    .filter(
      (rate): rate is number => typeof rate === 'number' && Number.isFinite(rate) && rate >= 0,
    );
  return rates.length ? rates.reduce((total, rate) => total + rate, 0) : undefined;
}

export function accountNextStep(account: RuntimeAccount, context: AccountContext) {
  switch (accountState(account, context)) {
    case 'unknown':
      return '等待账号状态更新';
    case 'attention':
      return '请检查账号登录或验证';
    case 'cooldown':
      return '冷却到期后重新检查';
    case 'idle':
      return '暂无待分配任务';
    case 'running':
      return context.paused ? '当前任务继续，暂停领取新任务' : '当前任务正在执行';
    default:
      return context.paused ? '实例已暂停领取新任务' : '等待队列调度';
  }
}
