import { describe, expect, it } from 'vitest';
import type { AccountTask, RuntimeAccount } from './contracts';
import { accountState, accountReasons, type AccountContext } from './accountState';

const context: AccountContext = { fresh: true, now: 100, pending: 12, paused: false };
const task = (stage: string, speed?: number): AccountTask => ({
  task_id: stage,
  title: stage,
  stage,
  speed,
  transfer_reserved: false,
});
const account = (value: Partial<RuntimeAccount> = {}): RuntimeAccount => ({
  id: 'a',
  name: 'Account A',
  known: true,
  state: 'ready',
  tasks: [],
  ...value,
});

describe('account workbench states', () => {
  it('keeps ongoing work running when admission is cooling or requires attention', () => {
    for (const state of ['cooldown', 'manual', 'unavailable']) {
      expect(
        accountState(
          account({
            state,
            tasks: [task('downloading'), task('waiting_processing')],
            reasons: [{ code: 'account_cooldown', label: '账号冷却中', until: 200 }],
          }),
          context,
        ),
      ).toBe('running');
    }
  });
  it('separates account cooldown from channel cooldown and normal spacing', () => {
    expect(
      accountState(
        account({
          tasks: [task('waiting_transfer')],
          reasons: [{ code: 'account_interval', label: '启动间隔', until: 200 }],
        }),
        context,
      ),
    ).toBe('waiting');
    expect(
      accountState(
        account({
          tasks: [
            {
              ...task('waiting_transfer'),
              wait: { code: 'transfer_channel_cooldown', label: '渠道冷却中', until: 200 },
            },
          ],
        }),
        context,
      ),
    ).toBe('waiting');
    for (const code of ['account_cooldown']) {
      expect(accountState(account({ reasons: [{ code, label: code, until: 200 }] }), context)).toBe(
        'cooldown',
      );
    }
  });
  it('distinguishes actual demand, manual intervention and unknown or expired samples', () => {
    expect(accountState(account(), context)).toBe('waiting');
    expect(accountState(account(), { ...context, pending: 0 })).toBe('idle');
    expect(accountState(account({ state: 'manual' }), context)).toBe('attention');
    expect(
      accountState(
        account({ reasons: [{ code: 'account_halted', label: '账号需处理' }] }),
        context,
      ),
    ).toBe('attention');
    expect(
      accountState(account({ tasks: [task('downloading')] }), { ...context, fresh: false }),
    ).toBe('unknown');
    expect(accountState(account({ known: false }), context)).toBe('unknown');
    expect(accountState(account({ state: 'cooldown', cooldown_until: 99 }), context)).toBe(
      'unknown',
    );
    expect(accountState(account({ state: 'recovery' }), { ...context, pending: 0 })).toBe('idle');
    expect(accountState(account({ tasks: [task('waiting_cookie')] }), context)).toBe('waiting');
  });
  it('keeps actionable admission restrictions but hides expired spacing and idle resource gates', () => {
    const value = account({
      reasons: [
        { code: 'account_interval', label: '间隔', until: 99 },
        { code: 'account_busy', label: '并发已满' },
        { code: 'account_cooldown', label: '账号池冷却', until: 200 },
        { code: 'paused', label: '实例暂停' },
      ],
    });
    expect(accountReasons(value, context).map((r) => r.code)).toEqual([
      'account_busy',
      'account_cooldown',
    ]);
    expect(accountReasons(value, { ...context, pending: 0 }).map((r) => r.code)).toEqual([
      'account_cooldown',
    ]);
  });
});
