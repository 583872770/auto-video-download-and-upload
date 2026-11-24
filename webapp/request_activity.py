"""Short request-phase activity in Redis; task/account ownership stays fenced.

No second connection budget: account assignment already bounds concurrency.
These expiring records replace per-request SQL permit insert/delete and router
locks. Redis failure delays a new request rather than silently bypassing it.
"""
import json
import uuid


class RequestActivity:
    def __init__(self, store):
        self.cache = store.progress_cache

    def enter(self, task, owner, instance, phase):
        token = uuid.uuid4().hex
        value = json.dumps(dict(task_id=task, owner=owner, instance=instance, phase=phase))
        key = self.cache.live_prefix + 'request:' + token
        if not self.cache._call('set', key, value, ex=120):
            from webapp.throughput import PipelineBusy
            error = PipelineBusy('请求调度 Redis 暂时不可用，等待恢复后继续；账号保护保持生效。')
            error.retry_after = 5
            raise error
        return key

    def leave(self, key):
        # Expiry bounds records if the worker or Redis disappears.
        self.cache._call('delete', key)
