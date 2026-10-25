"""Same-origin APIs; subscription credentials never go back to the browser."""
from flask import abort, jsonify, request

from webapp.clash import ClashError
from webapp.settings import save_settings


def register_clash(app, manager, store, media_lock, settings_lock, settings, config_path, sync_runtime):
    @app.errorhandler(ClashError)
    def clash_error(error):
        return jsonify(error=str(error)), 502

    def rotation_locked():
        return store.pending(runnable=True) or bool(store.download_queue()['running'])

    def idle(*, rotation=False, subscription=False):
        login = app.extensions.get('youtube_login')
        if login and login.status()['active']:
            abort(409, description='请先完成或取消 YouTube 登录，再修改代理或节点。')
        locked = rotation_locked()
        if locked:
            if subscription:
                abort(409, description='请先停止全部下载并等待当前任务结束，再更新订阅。')
            if rotation:
                abort(409, description='请先停止全部下载并等待当前任务结束，再修改轮换配置。')
            abort(409, description='请先停止全部下载并等待当前任务结束，再切换节点或停止代理。')

    def status():
        config, _ = settings()
        data = manager.status()
        rotation = data['rotation']
        completed = store.rotation_completed(rotation['id'], rotation['count_failures']) if rotation['id'] else 0
        rotation.update(completed=completed, remaining=rotation['every'] - completed % rotation['every'])
        rotation['next_node'] = (rotation['nodes'][(rotation['start_index'] + completed // rotation['every']) % len(rotation['nodes'])]
                                 if rotation['enabled'] and rotation['nodes'] else '')
        rotation['worker_count'] = config.get('worker_count', 1)
        rotation['workers'] = []
        if rotation['enabled']:
            for slot in range(config.get('worker_count', 1)):
                value = store.rotation_completed(rotation['id'] + (f':{slot}' if slot else ''), rotation['count_failures'])
                rotation['workers'].append({'slot': slot, 'completed': value,
                    'next_node': rotation['nodes'][(rotation['start_index'] + value // rotation['every']) % len(rotation['nodes'])]})
        login = app.extensions.get('youtube_login')
        login_active = bool(login and login.status()['active'])
        paused_changes_locked = rotation_locked() or login_active
        queue = store.download_queue()
        connection_reason = ('请先完成或取消 YouTube 登录，再应用当前连接。' if login_active else
                             '请先停止全部下载并等待当前任务结束，再应用当前连接。' if paused_changes_locked or not queue['paused'] else
                             '当前没有排队的下载或导入任务。' if not queue['queued'] else '')
        return {**data, 'locked': paused_changes_locked,
                'connection_edit_locked': paused_changes_locked,
                'connection_apply_locked': bool(connection_reason),
                'connection_apply_reason': connection_reason,
                'download_connection': store.download_connection(),
                'subscription_locked': paused_changes_locked,
                'subscription_lock_reason': '请先完成或取消 YouTube 登录，再更新订阅。' if login_active else
                                            '请先停止全部下载并等待当前任务结束，再更新订阅。' if paused_changes_locked else '',
                'rotation_locked': paused_changes_locked,
                'rotation_lock_reason': '请先完成或取消 YouTube 登录，再修改轮换配置。' if login_active else
                                        '请先停止全部下载并等待当前任务结束，再修改轮换配置。',
                'mode': config.get('proxy_mode', 'manual')}

    @app.get('/api/clash')
    def get_clash():
        return jsonify(status())

    @app.post('/api/downloads/connection')
    def apply_connection():
        with media_lock, settings_lock:
            idle(subscription=True)
            queue = store.download_queue()
            if not queue['paused'] or queue['running']:
                abort(409, description='请先停止全部下载并等待当前任务结束，再应用当前连接。')
            if not queue['queued']:
                abort(409, description='当前没有排队的下载或导入任务。')
            config, _ = settings()
            snapshot = manager.snapshot() if config.get('proxy_mode') == 'clash' else None
            try:
                connection = store.apply_download_connection(config, snapshot)
            except ValueError as error:
                abort(409, description=str(error))
        return jsonify(message='当前连接检查通过，待处理任务开始执行时会自动读取页面当前连接。',
                       download_connection=connection)

    @app.post('/api/clash/subscription')
    def subscription():
        request.max_content_length = 16384
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            raise ValueError('请提交订阅配置。')
        with media_lock:
            idle(subscription=True)
            count = manager.import_subscription(data.get('url', ''))
            with settings_lock:
                config, base = settings()
                config['proxy_mode'] = 'clash'
                save_settings(config_path, config)
                sync_runtime(config, base)
        return jsonify(message=f'订阅已更新，共 {count} 个节点，下载已启用 Clash 代理。', **status())

    @app.post('/api/clash/start')
    def start():
        with media_lock:
            idle()
            manager.start()
        return jsonify(status())

    @app.post('/api/clash/stop')
    def stop():
        with media_lock:
            idle()
            manager.stop()
        return jsonify(status())

    @app.post('/api/clash/select')
    def select():
        data = request.get_json(silent=True) or {}
        name = data.get('name') if isinstance(data, dict) else None
        if not isinstance(name, str):
            raise ValueError('请选择节点。')
        with media_lock:
            idle()
            manager.select(name)
        return jsonify(status())

    @app.post('/api/clash/rotation')
    def rotation():
        request.max_content_length = 512 * 1024
        data = request.get_json(silent=True)
        with media_lock:
            idle(rotation=True)
            manager.configure_rotation(data)
        return jsonify(message='自动轮换配置已保存。', **status())

    @app.post('/api/clash/delay')
    def delay():
        data = request.get_json(silent=True) or {}
        if not isinstance(data, dict) or (data.get('name') is not None and not isinstance(data['name'], str)):
            raise ValueError('测速参数不正确。')
        manager.test_delays(data.get('name'))
        return jsonify(status()), 202

    @app.post('/api/clash/test')
    def test():
        return jsonify(manager.test_youtube())
