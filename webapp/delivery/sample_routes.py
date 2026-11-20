"""Task results, bounded sample controls and instance-local protected previews."""
import json
import time
from pathlib import Path

from flask import abort, current_app, g, jsonify, request, send_file
from sqlalchemy import select
from werkzeug.exceptions import Conflict, Forbidden

from webapp.database import (tasks, delivery_runs as runs, delivery_work,
    delivery_artifacts as artifacts, test_samples as samples)
from webapp.delivery import samples as retention
from webapp.delivery.artifacts import task_details
from webapp.delivery.listing import PUBLIC_FIELDS
from webapp.disk_budget import DiskBudget, file_lock, safe_path


def register_samples(app, store):
    from webapp.jobs import Jobs, event
    jobs = Jobs(store)

    def body():
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            raise ValueError('请提交 JSON 对象。')
        return data

    def administrator():
        if (getattr(g, 'console_user', None) or {}).get('role', 'admin') != 'admin':
            raise Forbidden('留样设置和文件清理需要管理员权限。')

    @app.route('/api/jobs/<job>/sample-policy', methods=['GET', 'PUT'])
    def sample_policy(job):
        with store.connect() as db:
            jobs.require(db, job, editable=request.method != 'GET')
            value = retention.policy(db, job)
        if request.method == 'PUT':
            administrator()
            value = retention.save_policy(store, job, body())
            with store.connect() as db:
                event(store, db, job, '测试留样设置已更新：' + ('循环保留真实样本。' if value['enabled'] else '快速测试，校验后清理。'))
        return jsonify(value)

    @app.get('/api/jobs/<job>/samples')
    def job_samples(job):
        with store.connect() as db:
            jobs.require(db, job, editable=False)
            rows = db.execute(select(samples).where(samples.c.job_id == job,
                samples.c.state == 'ready').order_by(samples.c.created.desc()).limit(200)).mappings().all()
            value = retention.policy(db, job)
        return jsonify(items=[retention.public(row) for row in rows], policy=value,
            note='每个实例分别遵守此任务的留样限制，同时受实例总额度和空间保护限制；空间紧张时可提前回收。')

    @app.get('/api/jobs/<job>/videos/<task_id>/results')
    def results(job, task_id):
        with store.connect() as db:
            jobs.require(db, job, editable=False)
            task = db.execute(select(tasks).where(tasks.c.id == task_id, tasks.c.batch_id == job,
                tasks.c.action == 'download')).mappings().first()
            if not task:
                abort(404, description='视频不属于此任务。')
            delivery = task_details(db, [task_id])[task_id]
            spec = db.execute(select(runs.c.spec).where(runs.c.task_id == task_id)).scalar_one_or_none()
            ids = [r['artifact_id'] for r in delivery['requirements'] if r.get('artifact_id')]
            files = [dict(row) for row in db.execute(select(*PUBLIC_FIELDS).where(artifacts.c.id.in_(ids),
                artifacts.c.test_mode == 0)).mappings()] if ids else []
            kept = db.execute(select(samples).where(samples.c.task_id == task_id, samples.c.job_id == job,
                samples.c.state == 'ready').order_by(samples.c.created.desc()).limit(200)).mappings().all()
        for item in files:
            item['content_url'] = '/api/delivery/files/' + item['id']
        progress = json.loads(task['progress'] or '{}')
        return jsonify(task_id=task_id, video_id=task['video_id'], title=task['title'], status=task['status'],
            error=task['error'], delivery=delivery, files=files, samples=[retention.public(r) for r in kept],
            selected_outputs=json.loads(spec)['outputs'] if spec else [],
            retention=progress.get('sample_retention'), review=progress.get('sample_review'))

    @app.get('/api/jobs/<job>/observation')
    def observations(job):
        with store.connect() as db:
            jobs.require(db, job, editable=False)
        from webapp.delivery.observation import read
        return jsonify(read(store, job, request.args))

    @app.post('/api/jobs/<job>/samples/<sample_id>/action')
    def sample_action(job, sample_id):
        data = body()
        if data.get('action') not in ('accept', 'reject'):
            administrator()
        if data.get('action') == 'delete' and data.get('confirmed') is not True:
            raise ValueError('请先确认清理此样本，任务结果与统计会保留。')
        with store.connect() as db:
            jobs.require(db, job)
            row = db.execute(select(samples.c.instance_id).where(samples.c.id == sample_id,
                samples.c.job_id == job)).first()
        if not row:
            abort(404, description='样本已清理，请刷新。')
        data = {**data, 'job_id': job, 'actor': (g.console_user or {}).get('username', 'local')}
        if row.instance_id != retention.local_id(store):
            from webapp.adapters.fleet import rpc
            return jsonify(rpc(store.fleet, row.instance_id, f'/api/disk/samples/{sample_id}/action', method='POST', data=data))
        return jsonify(retention.action(store, DiskBudget(store.directory, store.runtime()), sample_id, data, data['actor']))

    @app.post('/api/disk/samples/<sample_id>/action')
    def local_sample_action(sample_id):
        # Only authenticated manager RPCs can use this low-level mutation route.
        if not getattr(g, 'fleet_rpc', False):
            abort(403)
        data = body()
        return jsonify(retention.action(store, DiskBudget(store.directory, store.runtime()), sample_id, data, str(data.get('actor', 'manager'))[:64]))

    @app.get('/api/disk/samples/<sample_id>/files/<artifact_id>')
    def sample_file(sample_id, artifact_id):
        with file_lock(store.directory / 'sample-maintenance.lock', blocking=True) as owned:
            if not owned:
                raise Conflict('样本正在整理，请稍后重试。')
            with store.connect() as db:
                row = db.execute(select(samples).where(samples.c.id == sample_id,
                    samples.c.instance_id == retention.local_id(store), samples.c.state == 'ready').with_for_update()).mappings().first()
                now = time.time()
                if not row or (row['expires'] < now and row['view_until'] < now):
                    abort(404, description='样本已到期或已清理，测试结果仍然保留。')
                item = next((f for f in json.loads(row['files']) if f['id'] == artifact_id), None)
                if not item:
                    abort(404)
                folder = safe_path(store.directory / 'test-samples' / row['id'], store.directory / 'test-samples')
                path = safe_path(folder / item['name'], folder)
                if not path.is_file():
                    abort(404, description='样本文件不可用，请重新下载并留样。')
                db.execute(samples.update().where(samples.c.id == sample_id).values(view_until=now + 120))
            from webapp.delivery.storage import content_type
            response = send_file(path, mimetype=content_type(path), as_attachment=request.args.get('download') == '1', conditional=True)
            response.headers['Cache-Control'] = 'private, no-store'
            return response

    @app.post('/api/jobs/<job>/videos/<task_id>/resample')
    def resample(job, task_id):
        data = body()
        if data.get('confirmed') is not True:
            raise ValueError('重新下载会产生网络请求，请先确认。')
        with store.connect() as db:
            if store.db.sqlite:
                db.exec_driver_sql('BEGIN IMMEDIATE')
            jobs.require(db, job)
            task = db.execute(select(tasks).where(tasks.c.id == task_id, tasks.c.batch_id == job,
                tasks.c.action == 'download').with_for_update()).mappings().first()
            run = db.execute(select(runs).where(runs.c.task_id == task_id)).mappings().first()
            if not task or task['status'] not in ('succeeded', 'failed', 'interrupted') or not run or not run['test_mode']:
                raise Conflict('仅已结束的测试视频可重新下载并留样。')
            if db.execute(select(delivery_work.c.task_id).where(delivery_work.c.task_id == task_id)).first():
                raise Conflict('仍有本地处理文件，请先在空间与日志中处理该失败测试。')
            payload = store.decode(task, private=True, db=db)['payload']
            payload['_force_test_sample'] = True
            # Follow the normal queue admission, account intervals and pinned contract.
            store._transition(db, task, 'queued')
            db.execute(tasks.update().where(tasks.c.id == task_id).values(status='queued',
                payload=store._pack(db, payload), worker_id=None, lease_until=None, attempts=0,
                available_at=0, error=None, result=None, finished_at=None, progress=None))
            from webapp.notifications import retry_task
            retry_task(store, db, task)
            event(store, db, job, f'视频 {task["video_id"]} 已安排重新下载并留样；沿用原要求和正常下载策略。')
        store.progress_cache.discard([task_id])
        runner = current_app.extensions.get('task_runner')
        if runner:
            runner.wake()
        return jsonify(message='已排队重新下载并留样；暂停中的任务需前往工作台开始。'), 202
