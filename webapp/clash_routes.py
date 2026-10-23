"""Same-origin APIs; subscription credentials never go back to the browser."""
from flask import abort, jsonify, request

from webapp.clash import ClashError
from webapp.settings import save_settings


def register_clash(app, manager, store, media_lock, settings_lock, settings, config_path):
    @app.errorhandler(ClashError)
    def clash_error(error):
        return jsonify(error=str(error)), 502

    def rotation_locked():
        return store.pending(runnable=True) or bool(store.download_queue()['running'])

    def idle(*, rotation=False):
        login = app.extensions.get('youtube_login')
        if login and login.status()['active']:
            abort(409, description='请先完成或取消 YouTube 登录，再修改代理或节点。')
        locked = rotation_locked() if rotation else store.pending()
        if locked:
            if rotation:
                abort(409, description='请先停止全部下载并等待当前任务结束，再修改轮换配置。')
            abort(409, description='请等待下载队列结束后再修改轮换配置、更新订阅、手动切换节点或停止代理。')

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
        return {**data, 'locked': store.pending() or login_active,
                'rotation_locked': rotation_locked() or login_active,
                'rotation_lock_reason': '请先完成或取消 YouTube 登录，再修改轮换配置。' if login_active else
                                        '请先停止全部下载并等待当前任务结束，再修改轮换配置。',
                'mode': config.get('proxy_mode', 'manual')}

    @app.get('/api/clash')
    def get_clash():
        return jsonify(status())

    @app.post('/api/clash/subscription')
    def subscription():
        request.max_content_length = 16384
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            raise ValueError('请提交订阅配置。')
        with media_lock:
            idle()
            count = manager.import_subscription(data.get('url', ''))
            with settings_lock:
                config, _ = settings()
                config['proxy_mode'] = 'clash'
                save_settings(config_path, config)
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
