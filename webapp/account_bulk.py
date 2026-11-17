"""Bounded account operations and a server-owned sequential browser login batch."""
import copy
import json
from pathlib import Path
import threading
import time
import uuid

from sqlalchemy import select
from werkzeug.exceptions import Conflict, HTTPException

from web_worker import worker_lock
from webapp.database import youtube_accounts
from webapp.runtime.supervisor import now


ACTIVE = {'running', 'stopping'}
PENDING = {'queued', 'waiting', 'running'}


def selection(value):
    if not isinstance(value, list) or not 1 <= len(value) <= 1000:
        raise ValueError('请选择 1～1000 个账号。')
    seen, result = set(), []
    for row in value:
        if (not isinstance(row, dict) or not isinstance(row.get('id'), str)
                or not row['id'] or len(row['id']) > 128 or type(row.get('version')) is not int
                or row['version'] < 1 or row['id'] in seen):
            raise ValueError('账号选择或资料版本无效，请刷新清单后重新选择。')
        seen.add(row['id'])
        result.append({'id': row['id'], 'version': row['version']})
    return result


def message(error):
    if isinstance(error, HTTPException):
        return error.description
    if isinstance(error, ValueError):
        return str(error)
    return '操作未完成，请检查实例连接或服务日志后重试。'


def bind_accounts(lib, rows, target, actor):
    with lib.store.connect() as db:
        if not lib.validate_instance(db, target):
            raise ValueError('请选择目标实例。')
    results = []
    for row in rows:
        item = {**row, 'state': 'succeeded', 'message': '所属实例已修改。'}
        try:
            lib.update(row['id'], {'version': row['version'], 'instance_id': target}, actor)
        except Exception as error:
            item.update(state='failed', message=message(error))
        results.append(item)
    return {'items': results, 'succeeded': sum(r['state'] == 'succeeded' for r in results),
            'failed': sum(r['state'] == 'failed' for r in results)}


def login_rows(lib, rows, fallback):
    with lib.store.connect() as db:
        fallback = lib.validate_instance(db, fallback)
        saved = {r['id']: r for r in db.execute(select(youtube_accounts.c.id, youtube_accounts.c.email,
            youtube_accounts.c.instance_id).where(youtube_accounts.c.id.in_([r['id'] for r in rows]))).mappings()}
    result = []
    for row in rows:
        record = saved.get(row['id'])
        if not record:
            raise ValueError('所选账号已被删除，请刷新后重新选择。')
        target = record['instance_id'] or fallback
        if not target:
            raise ValueError('存在未绑定实例的账号，请为这些账号选择登录实例。')
        result.append({**row, 'email': record['email'], 'instance_id': target,
                       'state': 'queued', 'message': '等待登录。'})
    return result


def check_rows(lib, rows):
    with lib.store.connect() as db:
        saved = {r['id']: r for r in db.execute(select(youtube_accounts.c.id, youtube_accounts.c.email,
            youtube_accounts.c.instance_id).where(youtube_accounts.c.id.in_([r['id'] for r in rows]))).mappings()}
    if any(row['id'] not in saved for row in rows):
        raise ValueError('所选账号已被删除，请刷新后重新选择。')
    return [{**row, 'email': saved[row['id']]['email'], 'instance_id': saved[row['id']]['instance_id'],
             'state': 'queued', 'message': '等待检测登录。'} for row in rows]


class AccountLoginBatch:
    interval = 1.0
    timeout = 660
    directory_name = 'account-login-batch'
    label = '批量登录'
    stop_message = '正在停止当前登录并取消后续账号，已经保存的 Cookie 保留。'

    def __init__(self, directory, start_login, read_login, cancel_login):
        self.directory = Path(directory) / self.directory_name
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / 'status.json'
        self.lock = threading.RLock()
        self.start_login, self.read_login, self.cancel_login = start_login, read_login, cancel_login
        self.thread = None

    def _read(self):
        return json.loads(self.path.read_text(encoding='utf-8')) if self.path.exists() else {
            'state': 'idle', 'items': [], 'message': f'尚未创建{self.label}。'}

    def _write(self, state):
        temporary = self.directory / (uuid.uuid4().hex + '.tmp')
        try:
            temporary.write_text(json.dumps(state, ensure_ascii=False), encoding='utf-8')
            temporary.replace(self.path)
        finally:
            temporary.unlink(missing_ok=True)

    def status(self):
        with self.lock:
            state = self._read()
            if state['state'] in ACTIVE:
                with worker_lock(self.directory / 'session.lock') as acquired:
                    if acquired:
                        state.update(state='interrupted', message=f'服务已重启，{self.label}已中断；请核对结果后重新选择未完成账号。', finished_at=now())
                        for row in state['items']:
                            if row['state'] in PENDING:
                                row.update(state='interrupted', message='服务已重启，请检查账号当前状态后重试。')
                        self._write(state)
            counts = {key: sum(row['state'] == key for row in state['items']) for key in
                      ('succeeded', 'failed', 'needs_attention', 'skipped', 'cancelled', 'interrupted')}
            return {**state, 'active': state['state'] in ACTIVE, 'total': len(state['items']),
                    'finished': sum(counts.values()), 'counts': counts}

    def start(self, rows, actor):
        with self.lock:
            guard = worker_lock(self.directory / 'session.lock')
            if not guard.__enter__():
                guard.__exit__(None, None, None)
                raise Conflict(f'已有{self.label}正在执行，请先查看或停止当前批次。')
            try:
                state = {'id': uuid.uuid4().hex, 'state': 'running', 'actor': actor, 'items': copy.deepcopy(rows),
                         'started_at': now(), 'finished_at': None, 'message': f'正在依次处理{self.label}。'}
                self._write(state)
                self.thread = threading.Thread(target=self._run, args=(state, guard), name='account-login-batch', daemon=True)
                self.thread.start()
            except BaseException:
                guard.__exit__(None, None, None)
                raise
            return self.status()

    def cancel(self, identity):
        with self.lock:
            state = self.status()
            if state.get('id') != identity:
                raise Conflict(f'{self.label}已变化，请刷新后重试。')
            if state['active']:
                (self.directory / (identity + '.cancel')).touch()
                state.update(state='stopping', message=self.stop_message)
                state.pop('active', None)
                self._write(state)
            return self.status()

    def _update(self, batch, row, **values):
        with self.lock:
            changed = any(row.get(key) != value for key, value in values.items())
            row.update(**values)
            if (self.directory / (batch['id'] + '.cancel')).exists():
                changed = changed or batch['state'] != 'stopping'
                batch['state'] = 'stopping'
            if changed:
                self._write(batch)

    def _one(self, state, row, cancelled):
        target, identity = row['instance_id'], row['id']
        deadline = time.monotonic() + self.timeout
        while True:
            if cancelled.exists():
                self._update(state, row, state='cancelled', message='已取消，未发起登录。')
                return
            current = self.read_login(target)
            if not current.get('active'):
                break
            self._update(state, row, state='waiting', message='实例有其他登录窗口，正在等待。')
            if time.monotonic() > deadline:
                raise ValueError('实例长时间被其他登录占用，请稍后重试。')
            time.sleep(self.interval)
        self._update(state, row, state='running', message='正在启动自动登录。')
        data = {'version': row['version'], 'instance_id': target, 'automatic': True,
                'automatic_only': True, 'batch_id': state['id']}
        try:
            current = self.start_login(identity, data, state['actor'])
        except Exception:
            # A timed-out POST may already have started remotely. Reconcile by
            # batch ID before deciding whether this attempt failed.
            current = self.read_login(target)
            if (current.get('account') or {}).get('batch_id') != state['id'] or (current.get('account') or {}).get('account_id') != identity:
                raise
        session = current.get('id')
        if not session or (current.get('account') or {}).get('account_id') != identity:
            raise ValueError('无法确认此账号的登录窗口，未操作其他会话。')
        failures = 0
        stop_sent = False
        deadline = time.monotonic() + self.timeout
        while True:
            if current.get('id') != session or (current.get('account') or {}).get('account_id') != identity:
                raise ValueError('登录窗口已被替换，请检查此账号结果；未取消其他登录。')
            if not current.get('active'):
                outcome = current.get('state')
                self._update(state, row, state=outcome if outcome in {'succeeded', 'needs_attention', 'cancelled'} else 'failed',
                             message=current.get('message', '登录未完成。'))
                return
            self._update(state, row, message=current.get('message', '正在登录。'), session_id=session)
            if (cancelled.exists() or time.monotonic() > deadline) and not stop_sent:
                self.cancel_login(target, identity, session)
                stop_sent = True
            if time.monotonic() > deadline + 45:
                raise ValueError('登录状态长时间无法确认，请检查实例后重试。')
            time.sleep(self.interval)
            try:
                current = self.read_login(target)
                failures = 0
            except Exception:
                failures += 1
                if failures >= 5:
                    raise ValueError('连续无法读取实例登录状态，请检查实例连接和此账号实际结果。') from None

    def _run(self, state, guard):
        cancelled = self.directory / (state['id'] + '.cancel')
        try:
            for row in state['items']:
                if cancelled.exists():
                    self._update(state, row, state='cancelled', message='已取消，未发起登录。')
                    continue
                try:
                    self._one(state, row, cancelled)
                except Exception as error:
                    self._update(state, row, state='failed', message=message(error))
            state.update(state='cancelled' if cancelled.exists() else 'completed', finished_at=now(),
                         message=f'{self.label}已停止。' if cancelled.exists() else f'{self.label}已完成，请查看各账号结果。')
        finally:
            with self.lock:
                try:
                    self._write(state)
                    cancelled.unlink(missing_ok=True)
                finally:
                    guard.__exit__(None, None, None)


class AccountCheckBatch(AccountLoginBatch):
    directory_name = 'account-check-batch'
    label = '批量检测'
    stop_message = '当前检测完成后停止，不再检测后续账号。'

    def __init__(self, directory, check):
        super().__init__(directory, None, None, None)
        self.check = check

    def _one(self, state, row, cancelled):
        self._update(state, row, state='running', message='正在所属实例检测登录。')
        result = self.check(row)
        self._update(state, row, **result)
