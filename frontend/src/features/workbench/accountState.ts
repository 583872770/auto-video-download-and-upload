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
function activeTask(task: AccountTask) {
  return !terminal.has(task.stage);
}

export function accountReasons(account: RuntimeAccount, context: AccountContext): WaitReason[] {
  const hasWork = context.pending > 0 || account.tasks.some(activeTask);
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
  if (account.tasks.some((task) => activeTask(task) && !isWaitingStage(task.stage)))
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
  if (context.pending > 0 || account.tasks.some((task) => isWaitingStage(task.stage)))
    return 'waiting';
  return 'idle';
}
