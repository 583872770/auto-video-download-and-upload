"""Business jobs own their list, execution controls and durable delivery ledger."""
import csv
from collections import Counter
from io import StringIO
import json
from pathlib import Path
import re
import uuid

from flask import Response, abort, jsonify, render_template, request, session, stream_with_context
from sqlalchemy import and_, func, or_, select
from werkzeug.exceptions import Conflict, PreconditionRequired

from webapp.database import (batches, counters, download_lists, imports, job_events,
    job_imports, job_profiles, job_removed, provider_tasks, tasks, instances, settings as schema_settings)
from webapp.fleet import POLICY_FIELDS
from webapp.settings import DEFAULTS, urls_from, validate_settings
from webapp.statistics import charge, pricing, stamp


def video_urls(value):
    from webapp.store import source_identity
    result = []
    for url in urls_from(value):
        identity, _ = source_identity(url)
        if not identity or not re.fullmatch(r'[A-Za-z0-9_-]{11}', identity):
            raise ValueError('任务清单仅支持 11 位 YouTube 视频 ID 或单个视频链接。')
        result.append('https://www.youtube.com/watch?v=' + identity)
    return result


def event(store, db, identity, message):
    db.execute(job_events.insert().values(id=uuid.uuid4().hex, seq=store.db.sequence(db, 'job_event'),
        job_id=identity, created_at=stamp(), message=message))


def profile(store, db, identity, *, lock=False):
    query = select(job_profiles).where(job_profiles.c.id == identity)
    return db.execute(query.with_for_update() if lock else query).mappings().first()


def apply_policy(store, db, task_id, payload):
    value = profile(store, db, payload.get('batch_id')) if payload.get('batch_id') else None
    if not value:
        return payload
    config = {**payload['config'], **json.loads(value['policy'])}
    from Utils.config import video_directory
    config['_job_media_root'] = str(video_directory(config, payload['base']))
    # A fresh attempt directory prevents a cancelled, remote executor from
    # modifying a later attempt's file before it notices its revoked lease.
    config['dir'] = str(Path(config.get('dir') or '') / 'jobs' / value['id'] / task_id / uuid.uuid4().hex[:12])
    return {**payload, 'config': config, '_business_job': value['id']}


def check_import(store, db, identity, revision):
    value = profile(store, db, identity, lock=True)
    if not value or value['deleted'] or value['archived'] or value['revision'] != revision:
        raise InterruptedError('任务清单已清空、归档或删除，本次旧导入已取消。')


class Jobs:
    def __init__(self, store):
        self.store = store

    def ensure(self, db, identity, config, data=None):
        value = profile(self.store, db, identity, lock=True)
        if value:
            return value
        if not db.execute(select(download_lists.c.id).where(download_lists.c.id == identity)).first():
            raise ValueError('历史批次没有独立账本，请新建任务。')
        values = dict(id=identity, note=(data or {}).get('note', ''),
            policy=json.dumps({key: config[key] for key in POLICY_FIELDS if key in config}, ensure_ascii=False),
            revision=1, edit_version=1, policy_version=1, archived=0, deleted=0,
            price_set=int(data is None or data.get('price') not in ('', None)), updated_at=stamp())
        db.execute(job_profiles.insert().values(**values))
        # Adopt an earlier one-import/one-list batch without losing its import
        # checkpoint or allowing that old payload to write after a clear.
        if db.execute(select(imports.c.id).where(imports.c.id==identity)).first():
            import_id=func.json_extract(tasks.c.payload,'$.import_id')
            if self.store.db.mysql:
                import_id=func.json_unquote(import_id)
            task_id=db.execute(select(tasks.c.id).where(tasks.c.action=='import',import_id==identity).limit(1)).scalar()
            self.store.db.ensure(db,job_imports,dict(id=identity,job_id=identity,revision=1,task_id=task_id))
        return values

    def require(self, db, identity, *, editable=True):
        row = db.execute(select(download_lists).where(download_lists.c.id == identity).with_for_update()).mappings().first()
        if not row:
            raise ValueError('任务不存在或没有独立账本。')
        value = profile(self.store, db, identity, lock=True)
        if value and value['deleted']:
            raise ValueError('任务已删除。')
        if editable and value and value['archived']:
            raise ValueError('任务已归档，请先恢复任务。')
        return row, value

    def create(self, data, config, base):
        customer = data.get('customer', '')
        name = data.get('name', data.get('list_name', ''))
        if not isinstance(customer, str) or not customer.strip() or not isinstance(name, str) or not name.strip():
            raise ValueError('请填写客户和清单名称。')
        if not isinstance(data.get('note', ''), str) or len(data.get('note', '')) > 2000:
            raise ValueError('备注最多 2000 个字符。')
        selected = data.get('policy', {})
        if not isinstance(selected, dict) or set(selected) - POLICY_FIELDS:
            raise ValueError('包含不支持的任务下载策略。')
        config = {**DEFAULTS, **config, **selected}
        if data.get('request_id'):
            try:
                request_id = uuid.UUID(str(data['request_id'])).hex
            except (ValueError, AttributeError):
                raise ValueError('无效的任务创建请求编号。') from None
        else:
            request_id = uuid.uuid4().hex
        # Use the creation UUID as the job identity. Retrying a lost HTTP
        # response cannot create a second customer's bill.
        identity = request_id
        validate_settings(config, download_only=True)
        urls = video_urls(data['urls']) if data.get('urls') else []
        with self.store.connect() as db:
            if self.store.db.sqlite:
                db.exec_driver_sql('BEGIN IMMEDIATE')
            self.store.db.ensure(db, schema_settings, {'key':'job_creation_lock','value':''})
            db.execute(select(schema_settings.c.key).where(schema_settings.c.key=='job_creation_lock').with_for_update()).first()
            existing = db.execute(select(download_lists.c.id).where(download_lists.c.id == identity)).first()
            if existing:
                return dict(id=identity, added=0, duplicates=0, reused=True)
            self.store.lists.create(db, identity, {**data, 'list_name': name,
                **({'price': '0'} if 'price' in data and data['price'] in ('', None) else {})})
            self.ensure(db, identity, config, data)
            ids = self.store.insert_downloads(db, [{'base': str(base), 'scope': 'download',
                'batch_id': identity, 'urls': [url]} for url in urls])
            event(self.store, db, identity, f'创建任务；新增 {len(ids)} 个视频。')
        return dict(id=identity, added=len(ids), duplicates=len(urls)-len(ids))

    def append(self, identity, urls, base):
        with self.store.connect() as db:
            self.require(db, identity)
            restored, pending = 0, []
            # Bounded batches avoid MySQL parameter limits and unbounded INs.
            for offset in range(0, len(urls), 1000):
                chunk = urls[offset:offset+1000]
                rows = db.execute(select(tasks).where(tasks.c.batch_id == identity,
                    tasks.c.action == 'download', tasks.c.url.in_(chunk)).with_for_update()).mappings().all()
                existing = {row['url']: row for row in rows}
                for url in chunk:
                    old = existing.get(url)
                    if not old:
                        pending.append(url)
                    elif old['status'] == 'removed':
                        previous = db.execute(select(job_removed.c.previous_status).where(job_removed.c.task_id == old['id'])).scalar_one()
                        state = 'succeeded' if previous == 'succeeded' else 'queued'
                        self.store._transition(db, old, state)
                        db.execute(tasks.update().where(tasks.c.id == old['id']).values(status=state, error=None,
                            finished_at=old['finished_at'] if state == 'succeeded' else None))
                        db.execute(job_removed.delete().where(job_removed.c.task_id == old['id']))
                        existing[url] = {**old, 'status': state}
                        restored += 1
            ids = self.store.insert_downloads(db, [{'base': str(base), 'scope': 'download',
                'batch_id': identity, 'urls': [url]} for url in pending])
            event(self.store, db, identity, f'追加清单：新增 {len(ids)}，恢复 {restored}，重复 {len(urls)-len(ids)-restored}。')
        return dict(added=len(ids), restored=restored, duplicates=len(urls)-len(ids)-restored)

    def page(self, query='', cursor=None, lifecycle='active', limit=20):
        condition = job_profiles.c.deleted == 1 if lifecycle == 'deleted' else and_(
            or_(job_profiles.c.deleted == 0, job_profiles.c.id.is_(None)),
            job_profiles.c.archived == 1 if lifecycle == 'archived' else or_(job_profiles.c.archived == 0, job_profiles.c.id.is_(None)))
        statement = select(download_lists.c.id, batches.c.seq).select_from(download_lists.join(batches,
            download_lists.c.id == batches.c.id).outerjoin(job_profiles, job_profiles.c.id == download_lists.c.id)).where(condition)
        if query:
            statement = statement.where(or_(download_lists.c.customer.contains(query, autoescape=True),
                download_lists.c.name.contains(query, autoescape=True), download_lists.c.id.startswith(query, autoescape=True)))
        if cursor:
            statement = statement.where(batches.c.seq < self.store.statistics.cursor(cursor))
        with self.store.connect() as db:
            rows = db.execute(statement.order_by(batches.c.seq.desc()).limit(limit+1)).all()
            ids = [row.id for row in rows[:limit]]
            if not ids:
                return dict(items=[], next_cursor=None)
            details = {row['id']: dict(row) for row in db.execute(select(download_lists, batches.c.created_at)
                .join(batches, batches.c.id == download_lists.c.id).where(download_lists.c.id.in_(ids))).mappings()}
            profiles = {row['id']: row for row in db.execute(select(job_profiles).where(job_profiles.c.id.in_(ids))).mappings()}
            counts = {}
            for scope, status, count in db.execute(select(counters).where(counters.c.scope.in_(['batch:' + key for key in ids]))):
                counts.setdefault(scope[6:], {})[status] = count
            import_counts = dict(db.execute(select(job_imports.c.job_id, func.count()).join(imports,
                imports.c.id == job_imports.c.id).where(job_imports.c.job_id.in_(ids), imports.c.finished == 0)
                .group_by(job_imports.c.job_id)).all())
            # Earlier imports may not yet have an adopted job_imports row.
            legacy = db.execute(select(imports.c.id).where(imports.c.id.in_(ids), imports.c.finished == 0,
                ~select(job_imports.c.id).where(job_imports.c.id == imports.c.id).exists())).scalars()
            for identity in legacy:
                import_counts[identity] = import_counts.get(identity, 0) + 1
            paused, available = self.execution_context(db)
            items = [self.describe(details[key], profiles.get(key), counts.get(key, {}),
                paused, available, import_counts.get(key, 0)) for key in ids]
        return dict(items=items, next_cursor=str(rows[limit-1].seq) if len(rows)>limit else None)

    def execution_context(self, db):
        available = True
        if self.store.fleet:
            from webapp.fleet import DOWNLOAD_ROLES, ONLINE_SECONDS
            available = bool(db.execute(select(instances.c.id).where(instances.c.role.in_(DOWNLOAD_ROLES),
                instances.c.paused == 0, instances.c.heartbeat > self.store.fleet.clock(db) - ONLINE_SECONDS).limit(1)).first())
        return self.store.downloads_paused(db), available

    @staticmethod
    def describe(row, value, counts, paused, available, importing):
        result = dict(row)
        for key in ('queued', 'running', 'succeeded', 'failed', 'interrupted', 'removed'):
            result[key] = counts.get(key, 0)
        result['total'] = sum(counts.get(key, 0) for key in ('queued', 'running', 'succeeded', 'failed', 'interrupted'))
        result['failed'] += result.pop('interrupted')
        result['completed'] = result['succeeded'] + result['failed']
        result['progress_percent'] = round(100*result['completed']/result['total'], 1) if result['total'] else None
        result.update(note=value['note'] if value else '', archived=bool(value and value['archived']),
            deleted=bool(value and value['deleted']), managed=bool(value),
            price_set=bool(value['price_set']) if value else True, revision=value['revision'] if value else 0,
            edit_version=value['edit_version'] if value else 0,
            policy_version=value['policy_version'] if value else 0, global_paused=paused, importing=importing,
            desired_running=bool(result['released']))
        result['amount'] = str(charge(result['file_bytes'], result['price'], result['unit'])) if result['price_set'] else None
        result['file_bytes_exact'] = str(result['file_bytes'])
        result['test_file_bytes_exact'] = str(result['test_file_bytes'])
        reason = ('系统已暂停领取' if paused else '没有在线且启用的下载实例' if not available else '')
        result['waiting_reason'] = reason if result['released'] and result['queued'] else ''
        result['state'] = ('deleted' if result['deleted'] else 'archived' if result['archived'] else
            'draining' if not result['released'] and result['running'] else 'running' if result['running'] else
            'importing' if importing else 'empty' if not result['total'] else
            ('waiting' if reason else 'queued') if result['released'] and result['queued'] else
            'paused' if result['queued'] else 'partial' if result['failed'] else 'completed')
        return result

    def detail(self, identity):
        with self.store.connect() as db:
            row = db.execute(select(download_lists, batches.c.created_at).join(batches,
                batches.c.id == download_lists.c.id).where(download_lists.c.id == identity)).mappings().first()
            if not row:
                return None
            value = profile(self.store, db, identity)
            counts = self.store._counts(db, ['batch:' + identity])
            paused, available = self.execution_context(db)
            running_videos = [self.store.decode(item) for item in db.execute(select(tasks).where(
                tasks.c.batch_id==identity,tasks.c.action=='download',tasks.c.status=='running').order_by(tasks.c.seq).limit(16)).mappings()]
            import_ids = select(job_imports.c.id).where(job_imports.c.job_id == identity)
            records = db.execute(select(imports).where(or_(imports.c.id.in_(import_ids),imports.c.id==identity)).order_by(imports.c.id).limit(100)).mappings().all()
            importing = db.execute(select(func.count()).select_from(imports).where(
                or_(imports.c.id.in_(import_ids),imports.c.id==identity), imports.c.finished == 0)).scalar_one()
            result = self.describe(row, value, counts, paused, available, importing)
            result.update(policy=json.loads(value['policy']) if value else None,
                imports=[dict(record) for record in records], running_videos=running_videos,
                transfer_speed=sum(float((item.get('progress') or {}).get('speed') or 0)
                    for item in running_videos if (item.get('progress') or {}).get('stage') == 'downloading'))
            return result

    def update(self, identity, data, config):
        with self.store.connect() as db:
            if self.store.db.sqlite:
                db.exec_driver_sql('BEGIN IMMEDIATE')
            row, old = self.require(db, identity)
            expected = data.get('edit_version')
            if type(expected) is not int:
                raise PreconditionRequired('请重新打开任务设置后保存；修改必须携带编辑版本。')
            if expected != (old['edit_version'] if old else 0):
                raise Conflict('任务已被其他页面修改。你的输入已保留，请查看最新版本并核对后重新保存。')
            value = old or self.ensure(db, identity, config)
            note = data.get('note', value['note'])
            if not isinstance(note, str) or len(note) > 2000:
                raise ValueError('备注最多 2000 个字符。')
            customer, name = data.get('customer', row['customer']), data.get('name', row['name'])
            if any(not isinstance(x, str) or not x.strip() or len(x) > 200 for x in (customer, name)):
                raise ValueError('客户和清单名称必填，最多 200 个字符。')
            price = data.get('price', row['price'])
            bill = pricing({'price': price if price not in ('', None) else '0', 'unit': data.get('unit', row['unit'])})
            values = dict(note=note, updated_at=stamp(), edit_version=value['edit_version'] + 1)
            if 'price' in data:
                values['price_set'] = int(price not in ('', None))
            policy = data.get('policy')
            if policy is not None:
                if not isinstance(policy, dict) or set(policy) - POLICY_FIELDS:
                    raise ValueError('不支持的任务策略。')
                previous = json.loads(value['policy'])
                merged = {**DEFAULTS, **config, **previous, **policy}
                validate_settings(merged, download_only=True)
                if merged.get('storage_mode', 'local') != previous.get('storage_mode', 'local') and db.execute(
                        select(tasks.c.id).where(tasks.c.batch_id == identity, tasks.c.started_at.is_not(None)).limit(1)).first():
                    raise ValueError('任务已经开始执行，请新建任务切换正式/测试模式。')
                values.update(policy=json.dumps({k: merged[k] for k in POLICY_FIELDS if k in merged}, ensure_ascii=False),
                    policy_version=value['policy_version'] + 1)
            db.execute(download_lists.update().where(download_lists.c.id == identity).values(customer=customer.strip(), name=name.strip(), **bill))
            db.execute(batches.update().where(batches.c.id == identity).values(name=name.strip()))
            db.execute(job_profiles.update().where(job_profiles.c.id == identity).values(**values))
            event(self.store, db, identity, '更新任务信息 / 策略；预计费用按整单单价重算。')

    def control(self, identity, action, config, data):
        with self.store.connect() as db:
            row, old = self.require(db, identity, editable=action not in ('delete', 'unarchive'))
            value = old or self.ensure(db, identity, config)
            if action == 'start':
                if not db.execute(select(tasks.c.id).where(tasks.c.batch_id == identity,
                        tasks.c.action == 'download', tasks.c.status == 'queued').limit(1)).first() and not db.execute(
                        select(imports.c.id).join(job_imports, imports.c.id == job_imports.c.id).where(
                        job_imports.c.job_id == identity, imports.c.finished == 0).limit(1)).first():
                    raise ValueError('任务没有待处理视频，请先追加视频或重试失败条目。')
                if self.store.media_clear_busy(db):
                    raise ValueError('素材库正在清空，请完成后再开始。')
                if self.store.fleet:
                    self.store.fleet.activate(db)
                db.execute(download_lists.update().where(download_lists.c.id == identity).values(released=1))
            elif action in ('pause', 'stop', 'archive', 'delete'):
                if action == 'delete' and data.get('confirmation') != row['name']:
                    raise ValueError('请输入完整任务名称确认删除。')
                db.execute(download_lists.update().where(download_lists.c.id == identity).values(released=0))
                if action in ('archive', 'delete'):
                    db.execute(job_profiles.update().where(job_profiles.c.id == identity).values(
                        archived=int(action=='archive'), deleted=int(action=='delete'), revision=value['revision']+1))
                    self.cancel_imports(db, identity)
                if action != 'pause':
                    rows = db.execute(select(tasks).where(tasks.c.batch_id == identity, tasks.c.action == 'download',
                        tasks.c.status == 'running').with_for_update()).mappings().all()
                    for task in rows:
                        self.store._transition(db, task, 'queued')
                        db.execute(tasks.update().where(tasks.c.id == task['id']).values(status='queued', worker_id=None,
                            lease_until=None, available_at=0, progress=None, attempts=max(0,task['attempts']-1)))
                        self.store.statistics.transition(db, task, 'paused')
            elif action == 'unarchive':
                db.execute(job_profiles.update().where(job_profiles.c.id == identity).values(archived=0))
            elif action == 'retry':
                rows = db.execute(select(tasks).where(tasks.c.batch_id == identity,
                    tasks.c.action == 'download', tasks.c.status.in_(('failed','interrupted'))).with_for_update()).mappings().all()
                for task in rows:
                    self.store._transition(db, task, 'queued')
                    db.execute(tasks.update().where(tasks.c.id == task['id']).values(status='queued', attempts=0,
                        error=None, finished_at=None, progress=None, available_at=0, worker_id=None, lease_until=None))
            else:
                raise ValueError('不支持的任务操作。')
            if action in ('archive', 'unarchive', 'delete'):
                db.execute(job_profiles.update().where(job_profiles.c.id == identity).values(
                    edit_version=value['edit_version']+1, updated_at=stamp()))
            event(self.store, db, identity, {'start':'开始任务','pause':'完成当前视频后暂停','stop':'立即停止当前任务',
                'archive':'归档任务','delete':'删除任务（保留账本与文件）','unarchive':'恢复归档任务','retry':'重新入队失败视频'}[action])
            self.store.statistics.settle(db)

    def cancel_imports(self, db, identity):
        ids = select(job_imports.c.id).where(job_imports.c.job_id == identity)
        db.execute(imports.update().where(or_(imports.c.id.in_(ids),imports.c.id==identity), imports.c.finished == 0).values(finished=-1))
        # Import tasks carry batch_id too, so their lease can be revoked without
        # scanning JSON or touching another customer's import lane.
        rows = db.execute(select(tasks).where(tasks.c.id.in_(select(job_imports.c.task_id).where(job_imports.c.job_id == identity)), tasks.c.action == 'import',
            tasks.c.status.in_(('queued','running'))).with_for_update()).mappings().all()
        for task in rows:
            self.store._transition(db, task, 'cancelled')
            db.execute(tasks.update().where(tasks.c.id == task['id']).values(status='cancelled', worker_id=None,
                lease_until=None, finished_at=stamp(), error='任务清单操作取消了导入。'))

    def remove(self, identity, data):
        with self.store.connect() as db:
            row, value = self.require(db, identity)
            clear = data.get('clear', False)
            if type(clear) is not bool:
                raise ValueError('清空标记必须为布尔值。')
            if clear and data.get('confirmation') != row['name']:
                raise ValueError('请输入完整任务名称确认清空。')
            if data.get('scope', 'pending') not in ('pending', 'all'):
                raise ValueError('请选择有效清空范围。')
            if clear:
                db.execute(download_lists.update().where(download_lists.c.id == identity).values(released=0))
                db.execute(job_profiles.update().where(job_profiles.c.id == identity).values(revision=value['revision']+1))
                self.cancel_imports(db, identity)
            ids = data.get('ids', [])
            if not clear and (not isinstance(ids, list) or not ids or len(ids)>1000 or any(not isinstance(x,str) for x in ids)):
                raise ValueError('每次请选择 1 到 1000 个视频条目。')
            condition = and_(tasks.c.batch_id == identity, tasks.c.action == 'download', tasks.c.status != 'removed')
            if not clear:
                condition = and_(condition, tasks.c.id.in_(ids))
            if clear and data.get('scope', 'pending') == 'pending':
                condition = and_(condition, tasks.c.status != 'succeeded')
            # Repeat bounded transactions would allow appended rows through the
            # operation; one transaction holds the job lock throughout instead.
            removed = 0
            while True:
                rows = db.execute(select(tasks).where(condition).order_by(tasks.c.seq).limit(500).with_for_update()).mappings().all()
                if not rows:
                    break
                db.execute(self.store.db.ignore(job_removed, [dict(task_id=task['id'],previous_status=task['status']) for task in rows]))
                changes = Counter(task['status'] for task in rows)
                for scope in sorted(('action:download','batch:'+identity)):
                    for status in sorted({*changes,'removed'}):
                        self.store._counter(db,scope,status,len(rows) if status=='removed' else -changes[status])
                db.execute(tasks.update().where(tasks.c.id.in_([task['id'] for task in rows])).values(
                    status='removed',worker_id=None,lease_until=None))
                for task in rows:
                    if task['status']=='running':
                        self.store.statistics.transition(db, task, 'paused')
                removed += len(rows)
            event(self.store, db, identity, f'{"清空" if clear else "移除"}清单：移除 {removed} 个视频；历史交付和费用保留。')
            self.store.statistics.settle(db)
        return removed


def register_jobs(app, store, settings, preflight):
    jobs = Jobs(store)

    def get(identity):
        value = jobs.detail(identity)
        if value is None or value['deleted']:
            abort(404, description='任务不存在或已删除。')
        return value

    def body():
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            raise ValueError('请提交 JSON 对象。')
        return data

    @app.get('/jobs')
    def jobs_page():
        return render_template('jobs.html', csrf=session.get('csrf',''), service_role=app.extensions['fleet'].role if app.extensions.get('fleet') else 'all')

    @app.get('/api/jobs')
    def job_page():
        lifecycle = request.args.get('lifecycle', 'active')
        if lifecycle not in ('active','archived'):
            raise ValueError('不支持的任务筛选。')
        return jsonify(jobs.page(request.args.get('q','')[:200],request.args.get('cursor'),lifecycle))

    @app.post('/api/jobs')
    def job_create():
        config, base = settings()
        return jsonify(jobs.create(body(),config,base)), 201

    @app.get('/api/jobs/summary')
    def job_summary():
        with store.connect() as db:
            active = ~select(job_profiles.c.id).where(job_profiles.c.id==download_lists.c.id,
                or_(job_profiles.c.deleted==1,job_profiles.c.archived==1)).exists()
            from webapp.billing import BillingService
            billing = BillingService(store).summary(db)
            total=db.execute(select(func.count()).select_from(download_lists).where(active)).scalar_one()
            running=db.execute(select(func.count()).select_from(counters.join(download_lists,
                counters.c.scope=='batch:'+download_lists.c.id)).where(counters.c.status=='running',counters.c.value>0,active)).scalar_one()
            return jsonify(total=total, running=running, **billing, global_paused=store.downloads_paused(db))

    @app.post('/api/jobs/system/resume')
    def job_system_resume():
        # Restore only jobs already released; never release freshly imported lists.
        return jsonify(app.extensions['execution_control'].resume_system())

    @app.get('/api/jobs/system/status')
    def job_system_status():
        return jsonify(app.extensions['execution_control'].status())

    @app.get('/api/jobs/<identity>')
    def job_detail(identity):
        return jsonify(get(identity))

    @app.put('/api/jobs/<identity>')
    def job_update(identity):
        data=body()
        config,_=settings()
        jobs.update(identity, data, config)
        return jsonify(get(identity))

    @app.get('/api/jobs/<identity>/videos')
    def job_videos(identity):
        get(identity)
        state=request.args.get('status')
        if state not in (None,'queued','running','succeeded','failed','interrupted','removed'):
            raise ValueError('不支持的视频状态。')
        statement=select(tasks).where(tasks.c.batch_id==identity,tasks.c.action=='download')
        if state:
            statement=statement.where(tasks.c.status==state)
        else:
            statement=statement.where(tasks.c.status!='removed')
        q=request.args.get('q','')[:200]
        if q:
            statement=statement.where(or_(tasks.c.video_id.startswith(q,autoescape=True),tasks.c.title.contains(q,autoescape=True)))
        if request.args.get('cursor'):
            statement=statement.where(tasks.c.seq>store.statistics.cursor(request.args['cursor']))
        with store.connect() as db:
            rows=db.execute(statement.order_by(tasks.c.seq).limit(51)).mappings().all()
        return jsonify(items=[store.decode(row) for row in rows[:50]],next_cursor=str(rows[49]['seq']) if len(rows)>50 else None)

    @app.post('/api/jobs/<identity>/videos')
    def job_append(identity):
        get(identity)
        config,base=settings()
        with store.connect() as db:
            jobs.ensure(db,identity,config)
        return jsonify(jobs.append(identity,video_urls(body().get('urls')),base)),202

    @app.post('/api/jobs/<identity>/videos/remove')
    def job_remove(identity):
        get(identity)
        config,_=settings()
        with store.connect() as db:
            jobs.ensure(db,identity,config)
        return jsonify(removed=jobs.remove(identity,body()))

    @app.post('/api/jobs/<identity>/control')
    def job_control(identity):
        current=get(identity)
        data=body()
        config,base=settings()
        jobs.control(identity, data.get('action'), config, data)
        warnings = []
        if data.get('action') == 'start':
            from webapp.execution import isolate_download_failure
            from webapp.environment import DownloadEnvironmentRequired
            from webapp.yt5s import BrowserEnvironmentRequired
            from webapp.youtube_auth import YouTubeAuthRequired
            try:
                warnings = preflight({**config, **(current['policy'] or {})}, base) or []
            except (ValueError, DownloadEnvironmentRequired, BrowserEnvironmentRequired, YouTubeAuthRequired) as error:
                isolate_download_failure(store, error)
                warnings = [f'本机已暂停领取，任务运行意愿保留：{error}']
        return jsonify(message='任务操作已保存。', job=jobs.detail(identity), warnings=warnings)

    @app.post('/api/jobs/<identity>/imports')
    def job_import(identity):
        get(identity)
        request.max_content_length=1024**3+1024**2
        source=request.files.get('file')
        if not source or not source.filename or Path(source.filename).suffix.lower() not in ('.txt','.csv'):
            raise ValueError('请选择 UTF-8 的 TXT / CSV 文件。')
        config,base=settings()
        import_id=uuid.uuid4().hex
        path=store.directory/'imports'/(import_id+Path(source.filename).suffix.lower())
        path.parent.mkdir(parents=True,exist_ok=True)
        try:
            source.save(path)
            if not path.stat().st_size or path.stat().st_size>1024**3:
                raise ValueError('文件不能为空，最大 1 GB。')
            with store.connect() as db:
                jobs.require(db,identity)
                value=jobs.ensure(db,identity,config)
                db.execute(job_imports.insert().values(id=import_id,job_id=identity,revision=value['revision']))
                db.execute(imports.insert().values(id=import_id,path=str(path),name=Path(source.filename).name[:200],
                    kind=path.suffix[1:],offset=0,column_index=0,processed=0,added=0,duplicates=0,invalid=0,samples='[]',finished=0))
                task_id=store._create(db,'import',{'base':str(base),'scope':'download',
                    'import_id':import_id,'_job_id':identity,'_job_revision':value['revision']},store.db.sequence(db,'task'))
                db.execute(job_imports.update().where(job_imports.c.id==import_id).values(task_id=task_id))
                event(store,db,identity,'导入文件：'+Path(source.filename).name[:200])
        except Exception:
            path.unlink(missing_ok=True)
            raise
        return jsonify(id=task_id,import_id=import_id),202

    @app.get('/api/jobs/<identity>/statistics')
    def job_statistics(identity):
        current=get(identity)
        with store.connect() as db:
            providers=[dict(row) for row in db.execute(select(provider_tasks.c.method,provider_tasks.c.status,
                func.count().label('count')).join(tasks,tasks.c.id==provider_tasks.c.task_id).where(tasks.c.batch_id==identity)
                .group_by(provider_tasks.c.method,provider_tasks.c.status)).mappings()]
            events=[dict(row) for row in db.execute(select(job_events).where(job_events.c.job_id==identity)
                .order_by(job_events.c.seq.desc()).limit(50)).mappings()]
        return jsonify(job=current,providers=providers,events=events,files=store.lists.files(identity,limit=50),
            test_files=store.lists.files(identity,test=True,limit=50))

    @app.get('/api/jobs/<identity>/export')
    def job_export(identity):
        current=get(identity)
        kind=request.args.get('kind','ids')
        if kind not in ('ids','failed','bill'):
            raise ValueError('请选择有效导出类型。')
        from webapp.statistics_routes import csv_cell
        @stream_with_context
        def generate():
            yield '\ufeff'
            buf=StringIO(newline='')
            writer=csv.writer(buf)
            def line(values):
                buf.seek(0);buf.truncate()
                writer.writerow([csv_cell(x) for x in values])
                return buf.getvalue()
            if kind=='bill':
                yield line(['任务编号','客户','清单名称','计费字节','测试字节','单价','单位','预计费用'])
                yield line([identity,current['customer'],current['name'],current['file_bytes_exact'],
                    current['test_file_bytes_exact'],current['price'] if current['price_set'] else '',current['unit'],current['amount']])
                return
            yield line(['视频ID','状态','错误原因'])
            cursor=0
            while True:
                statement=select(tasks.c.seq,tasks.c.video_id,tasks.c.status,tasks.c.error).where(
                    tasks.c.batch_id==identity,tasks.c.action=='download',tasks.c.seq>cursor,tasks.c.status!='removed')
                if kind=='failed':
                    statement=statement.where(tasks.c.status.in_(('failed','interrupted')))
                with store.connect() as db:
                    rows=db.execute(statement.order_by(tasks.c.seq).limit(1000)).all()
                for row in rows:
                    yield line([row.video_id,row.status,row.error])
                    cursor=row.seq
                if len(rows)<1000:
                    break
        return Response(generate(),mimetype='text/csv',headers={'Content-Disposition':f'attachment; filename="job-{identity}-{kind}.csv"'})
