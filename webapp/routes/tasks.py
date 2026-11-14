"""Tasks HTTP endpoints."""
from flask import jsonify, request
from webapp.helpers import get_body, json_result


def register_tasks_routes(context, queue, preflight):
    app = context.app
    activate_downloads = preflight.activate_downloads
    startup = context.startup
    store = context.store
    body = get_body

    @app.get('/api/tasks')
    def tasks():
        actions = ('download', 'check', 'import', 'index', 'clear_media') if request.args.get('scope') == 'download' else None
        active = request.args.get('view') == 'active'
        if active:
            actions = ('download',)
        batch_id = request.args.get('batch_id')
        return jsonify(**store.page(actions=actions, limit=int(request.args.get('limit', 50)),
                       cursor=request.args.get('cursor'), status='active' if active else request.args.get('status'),
                       finished_only=request.args.get('view') == 'history',
                       query=request.args.get('q', '')[:200], batch_id=batch_id, order=request.args.get('order', 'desc')),
                       counts=store.counts(actions=actions, batch_id=batch_id))

    @app.get('/api/batches')
    def download_batches():
        return jsonify(store.batch_page(cursor=request.args.get('cursor')))

    @app.post('/api/downloads/stop')
    def stop_downloads():
        return json_result(queue.stop_downloads())

    @app.post('/api/downloads/resume')
    def resume_downloads():
        return json_result(queue.resume_downloads())

    @app.get('/api/downloads/startup')
    def startup_status():
        return jsonify(startup.status())

    @app.post('/api/downloads/start')
    def start_downloads():
        return jsonify(startup.start(activate_downloads)), 202

    @app.post('/api/downloads/import')
    def import_downloads():
        config, _ = context.settings()
        request.max_content_length = int(config.get('disk_upload_limit_mb', 256)) * 1024 ** 2 + 1024 ** 2
        return json_result(queue.import_downloads(request.files.get('file'), request.form, config))

    @app.get('/api/tasks/<task_id>')
    def task_detail(task_id):
        task = store.get(task_id)
        if task:
            from webapp.risk_diagnostics import task_risks
            task['risk'] = task_risks(task)
        return (jsonify(task), 200) if task else (jsonify(error='任务不存在'), 404)

    @app.get('/api/tasks/<task_id>/logs')
    def task_logs(task_id):
        task = store.get(task_id)
        if not task:
            return jsonify(error='任务不存在'), 404
        try:
            offset = int(request.args.get('offset', 0))
        except ValueError:
            raise ValueError('日志偏移量必须为整数。')
        result = store.logs(task_id, offset)
        if result.get('state') == 'missing':
            result['state'] = ('pruned' if task.get('logs_pruned') else
                               'pending' if task['status'] in ('queued', 'running') else 'missing')
            result['message'] = {'pruned': '日志已按容量或保留期限自动清理，任务结果和统计仍可查看。',
                                 'pending': '尚未产生日志，请等待任务开始执行。',
                                 'missing': '本实例未找到任务日志，请查看实例运行日志及磁盘写入告警。'}[result['state']]
        return jsonify(result)

    @app.post('/api/tasks')
    @app.post('/api/downloads')
    def create_task():
        return json_result(queue.create_task(body(), batch=request.path == '/api/downloads'))

    @app.post('/api/downloads/queue')
    def enqueue_downloads():
        return json_result(queue.enqueue_downloads(body()))
