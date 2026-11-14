"""Authenticated output listing, preview, missing-item acceptance and repair."""
import csv
from io import StringIO
import json
from pathlib import Path
from flask import Response, abort, g, jsonify, request, send_file, stream_with_context
from sqlalchemy import select
from werkzeug.exceptions import Conflict
from webapp.database import delivery_artifacts as artifacts, delivery_requirements as requirements, delivery_runs as runs, tasks
from webapp.delivery import artifacts as ledger
from webapp.delivery.contracts import DEFAULT, normalize, packed
from webapp.delivery.listing import page as artifact_page
from webapp.statistics import stamp


def register_delivery(app, store, settings):
    from webapp.jobs import Jobs, event
    jobs = Jobs(store)

    def body():
        value = request.get_json(silent=True)
        if not isinstance(value, dict):
            raise ValueError('请提交 JSON 对象。')
        return value

    @app.get('/api/delivery/defaults')
    def delivery_defaults():
        from webapp.database import settings as schema_settings
        with store.connect() as db:
            raw = db.execute(select(schema_settings.c.value).where(schema_settings.c.key == 'delivery_defaults')).scalar_one_or_none()
        return jsonify(spec=normalize(json.loads(raw)) if raw else normalize(DEFAULT))

    @app.put('/api/delivery/defaults')
    def delivery_save_defaults():
        if not getattr(g, 'console_local_access', False) and (not g.console_user or g.console_user['role'] != 'admin'):
            abort(403)
        spec = normalize(body())
        from webapp.database import settings as schema_settings
        with store.connect() as db:
            db.execute(store.db.upsert(schema_settings, dict(key='delivery_defaults', value=packed(spec)), ['key']))
        return jsonify(spec=spec)

    @app.get('/delivery-files')
    def delivery_files_page():
        from webapp.frontend import render_workspace
        page = render_workspace(app)
        if page is not None:
            return page
        from flask import render_template, session
        return render_template('delivery_files.html', csrf=session.get('csrf', ''), service_role='hybrid')

    def listing(job=None):
        return jsonify(**artifact_page(store, job=job, kind=request.args.get('kind', ''),
                                       language=request.args.get('language', ''),
                                       instance_id=request.args.get('instance_id', ''),
                                       query=request.args.get('q', ''), cursor=request.args.get('cursor'),
                                       limit=int(request.args.get('limit', 50))))

    @app.get('/api/delivery/artifacts')
    def delivery_files():
        if getattr(g, 'fleet_rpc', False):
            abort(403)
        return listing()

    @app.get('/api/jobs/<identity>/artifacts')
    def job_files(identity):
        with store.connect() as db:
            jobs.require(db, identity, editable=False)
        return listing(identity)

    @app.get('/api/delivery/files/<identity>')
    def delivery_content(identity):
        with store.connect() as db:
            row = db.execute(select(artifacts).where(artifacts.c.id == identity, artifacts.c.test_mode == 0)).mappings().first()
        if not row or not row['available']:
            abort(404, description='交付文件不存在或已清理。')
        local_id = store.fleet.id if store.fleet else 'local'
        if row['instance_id'] != local_id:
            if getattr(g, 'fleet_rpc', False):
                abort(403)
            from webapp.delivery.storage import remote_content
            return remote_content(store.fleet, row['instance_id'])
        if row['storage'] == 'oss':
            from Utils.oss_storage import OSSStorage
            config, _ = settings()
            storage = OSSStorage(config)
            size = row['size']
            start, stop = 0, size
            status = 200
            headers = {'Accept-Ranges': 'bytes'}
            if request.headers.get('Range'):
                from werkzeug.http import parse_range_header
                requested = parse_range_header(request.headers['Range'])
                selected = requested.range_for_length(size) if requested and requested.units == 'bytes' else None
                if not selected:
                    return Response(status=416, headers={'Content-Range': f'bytes */{size}'})
                start, stop = selected
                status = 206
                headers['Content-Range'] = f'bytes {start}-{stop - 1}/{size}'
            headers['Content-Length'] = str(stop - start)
            headers['Content-Disposition'] = ('attachment' if request.args.get('download') == '1' else 'inline') + '; filename="' + row['id'] + Path(row['path']).suffix + '"'
            import mimetypes
            mime = mimetypes.guess_type(row['path'])[0] or 'application/octet-stream'
            if request.method == 'HEAD':
                return Response(status=status, mimetype=mime, headers=headers)
            try:
                obj = storage.bucket.get_object(row['object_key'], byte_range=(start, stop - 1))
            except Exception:
                abort(404, description='云端交付文件不可用。')
            def stream():
                try:
                    while True:
                        chunk = obj.read(1024 * 1024)
                        if not chunk:
                            break
                        yield chunk
                finally:
                    obj.close()
            result = Response(stream_with_context(stream()), status=status, mimetype=mime, headers=headers)
            result.call_on_close(obj.close)
            return result
        path = Path(row['path'])
        if path.resolve() != path.absolute() or path.is_symlink() or not path.is_file():
            abort(404, description='本地交付文件已清理，请补交。')
        return send_file(path, as_attachment=request.args.get('download') == '1', conditional=True)

    @app.post('/api/jobs/<identity>/redeliver')
    def redeliver(identity):
        data = body();ids = data.get('ids')
        if type(data.get('use_latest', False)) is not bool:
            raise ValueError('使用最新交付要求的标记必须是布尔值。')
        if not isinstance(ids, list) or not 1 <= len(ids) <= 1000 or any(not isinstance(i, str) for i in ids):
            raise ValueError('每次请选择 1 到 1000 条来源进行补交。')
        with store.connect() as db:
            if store.db.sqlite:
                db.exec_driver_sql('BEGIN IMMEDIATE')
            jobs.require(db, identity)
            contract = ledger.current(db, identity)
            if not contract:
                raise ValueError('历史任务请先设置交付要求再补交。')
            rows = list(db.execute(select(tasks).where(tasks.c.batch_id == identity, tasks.c.id.in_(ids), tasks.c.action == 'download').with_for_update()).mappings())
            if len(rows) != len(set(ids)) or any(r['status'] in ('running', 'removed') for r in rows):
                raise Conflict('部分来源正在执行、已移除或不属于该客户任务。')
            for row in rows:
                if data.get('use_latest') is True:
                    run = db.execute(select(runs).where(runs.c.task_id == row['id']).with_for_update()).mappings().first()
                    if run:
                        ledger.state(store, db, run, 'pending')
                        db.execute(runs.update().where(runs.c.task_id == row['id']).values(version=contract['version'], spec=packed(contract['spec'])))
                        db.execute(requirements.delete().where(requirements.c.task_id == row['id']))
                    else:
                        ledger.pin(store, db, row['id'], identity, force=True)
                if row['status'] != 'queued':
                    store._transition(db, row, 'queued')
                db.execute(tasks.update().where(tasks.c.id == row['id']).values(status='queued', worker_id=None,
                    lease_until=None, attempts=0, available_at=0, error=None, result=None, finished_at=None))
            event(store, db, identity, f'补交 {len(rows)} 条来源，仅获取缺失产物；要求版本' + ('使用最新。' if data.get('use_latest') is True else '沿用原要求。'))
        return jsonify(queued=len(rows)), 202

    @app.post('/api/jobs/<identity>/requirements/<task_id>/<key>/waive')
    def waive(identity, task_id, key):
        reason = body().get('reason', '')
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 1000:
            raise ValueError('请填写 1 到 1000 字的确认原因。')
        with store.connect() as db:
            if store.db.sqlite:
                db.exec_driver_sql('BEGIN IMMEDIATE')
            jobs.require(db, identity)
            task = db.execute(select(tasks).where(tasks.c.id == task_id, tasks.c.batch_id == identity).with_for_update()).mappings().first()
            if not task or task['status'] == 'running':
                raise Conflict('来源不存在或正在执行，请停止后再确认。')
            row = db.execute(select(requirements).where(requirements.c.task_id == task_id, requirements.c.key == key).with_for_update()).mappings().first()
            if not row or row['status'] != 'missing':
                raise Conflict('该产物当前不是已确认的来源缺失项。')
            db.execute(requirements.update().where(requirements.c.task_id == task_id, requirements.c.key == key).values(
                status='waived', waived_by=(g.console_user or {}).get('username', 'local'), waived_reason=reason.strip(), updated_at=stamp()))
            status = ledger.refresh(store, db, task_id)
            event(store, db, identity, f'接受来源 {task["video_id"]} 缺少 {row["kind"]} {row["language"]}；原因：{reason.strip()}')
        return jsonify(status=status)

    @app.get('/api/jobs/<identity>/artifacts/export')
    def export_artifacts(identity):
        with store.connect() as db:
            jobs.require(db, identity, editable=False)
        from webapp.statistics_routes import csv_cell
        @stream_with_context
        def generate():
            yield '\ufeff'
            buffer = StringIO();writer = csv.writer(buffer)
            def line(values):
                buffer.seek(0);buffer.truncate();writer.writerow([csv_cell(v) for v in values]);return buffer.getvalue()
            yield line(['来源ID', '标题', '类型', '语言', '字节', '位置', '完成时间'])
            cursor = 0
            while True:
                with store.connect() as db:
                    rows = list(db.execute(select(artifacts).where(artifacts.c.job_id == identity,
                        artifacts.c.test_mode == 0, artifacts.c.available == 1, artifacts.c.seq > cursor).order_by(artifacts.c.seq).limit(500)).mappings())
                for row in rows:
                    yield line([row['video_id'], row['title'], row['kind'], row['language'], row['size'], row['storage'], row['created_at']])
                    cursor = row['seq']
                if len(rows) < 500:
                    break
        return Response(generate(), mimetype='text/csv', headers={'Content-Disposition': 'attachment; filename="delivery-' + identity + '.csv"'})
