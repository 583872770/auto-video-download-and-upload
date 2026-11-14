"""Failure evidence and task/account settlement without queue admission."""
from webapp.diagnostics import event, error_details
from webapp.download_policy import failure_evidence, recovery_policy


class TaskRecovery:
    def __init__(self, store, task, config, runtime, progress, lease_guard):
        self.store = store
        self.task = task
        self.config = config
        self.runtime = runtime
        self.progress = progress
        self.lease_guard = lease_guard

    def record_failure(self, error):
        store = self.store
        task = self.task
        last_progress = self.progress
        evidence = failure_evidence(error)
        last_progress['last_failure'] = evidence.public()
        store.update_progress(task['id'], dict(last_progress), store.owner)

    def settle_account_failure(self, error):
        store = self.store
        task = self.task
        config = self.config
        runtime = self.runtime
        last_progress = self.progress
        lease_guard = self.lease_guard
        from webapp.execution import isolate_download_failure
        failure = failure_evidence(error)
        policy = recovery_policy(failure, {**runtime, **config})
        if policy.targets == ('session',):
            if policy.manual:
                reason = (f'账号「{failure.profile_name or failure.profile_id}」需要处理登录或网页验证；'
                          '该账号暂停新增请求，任务绑定原账号等待，其他健康账号继续。'
                          '处理后在账号管理点击“检测登录”，检查通过即可恢复该账号。')
                store.update_progress(task['id'], {**last_progress, 'stage': 'retrying',
                    'speed': None, 'eta': None, 'retry_delay_seconds': 30,
                    'retry_reason': 'youtube_session_auth_required'}, store.owner)
                lease_guard.settle(store.finish, task['id'], error=reason, worker_id=store.owner,
                                   retryable=True, retry_after=30)
                event('account_auth_settled', reason='youtube_session_auth_required',
                      instance_paused=False, **error_details(error))
                print(reason, flush=True)
                return False
            delay = max(policy.delay, getattr(error, '_download_recovery_delay', 0), failure.retry_after or 0)
            wait = f'至少 {int(delay)} 秒'
            reason = (f'账号「{failure.profile_name or failure.profile_id}」限流，'
                      f'该账号冷却{wait}；任务绑定原账号等待，其他 Cookie 按各自额度继续。')
            store.update_progress(task['id'], {**last_progress, 'stage': 'retrying',
                'speed': None, 'eta': None, 'retry_delay_seconds': delay,
                'retry_reason': 'youtube_session_rate_limit'}, store.owner)
            lease_guard.settle(store.finish, task['id'], error=reason, worker_id=store.owner,
                               retryable=True, retry_after=delay)
            event('account_limit_settled', reason='youtube_session_rate_limit', retry_after=delay,
                  attempts=task['attempts'], max_attempts=task['max_attempts'],
                  retry_scheduled=task['attempts'] < task['max_attempts'],
                  instance_paused=False, **error_details(error))
            print(reason, flush=True)
            return False
        delay = max(policy.delay, getattr(error, 'retry_after', 0) or 0,
                    getattr(error, '_download_recovery_delay', 0))
        reason = ('YouTube 明确提示账号被限流，实例已停止领取，任务保留。'
                  '至少冷却一小时后再人工检查；登录有效不代表没有限额。')
        isolate_download_failure(store, error)
        store.update_progress(task['id'], {**last_progress, 'stage': 'retrying',
            'speed': None, 'eta': None, 'retry_delay_seconds': delay,
            'retry_reason': 'youtube_account_rate_limit'}, store.owner)
        lease_guard.settle(store.release, task['id'], store.owner, reason, delay=delay)
        event('attempt_released', reason='youtube_account_rate_limit',
              retry_after=delay, instance_paused=True, **error_details(error))
        print(reason, flush=True)
        return True


