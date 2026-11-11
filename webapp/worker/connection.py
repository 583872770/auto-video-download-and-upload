"""Select the execution proxy and filter cooling routes before provider attempts."""
from webapp.diagnostics import event


def prepare_connection(config, payload, channels, methods, progress, *, worker_slot,
                       rotation_completed, route_key):
    route_health = None
    snapshot = None
    if config.get('proxy_mode') == 'clash':
        progress.state.update(stage='preparing_proxy')
        progress.save(force=True)
        from webapp.clash import ClashManager
        proxy = payload.get('clash')
        if not proxy:
            raise RuntimeError('下载任务缺少代理节点配置，请重新创建。')
        manager = ClashManager(proxy['directory'])
        # Paused queues may contain millions of old snapshots. Resolve the
        # saved rotation here so changes also apply to existing queued work.
        manager.reload()
        proxy = dict(proxy) if proxy.get('node') else manager.snapshot()
        previous_rotation = proxy.pop('rotation', None)
        current_rotation = manager.rotation()
        if current_rotation['enabled']:
            proxy['rotation'] = current_rotation
        elif previous_rotation or proxy.get('subscription_id', '') != manager.config.get('subscription_id', ''):
            # Subscription updates while paused may replace the saved node.
            # Use the current selection without rewriting millions of tasks.
            proxy['node'] = manager.config.get('selected') or proxy['node']
        if worker_slot:
            manager = manager.for_worker(worker_slot)
            if not proxy.get('rotation'):
                manager.select(proxy['node'], automatic=True)
        rotation = proxy.get('rotation')
        completed = 0
        if rotation:
            if rotation_completed is None:
                raise RuntimeError('自动轮换需要通过下载队列执行。')
            rotation_id = rotation['id'] + (f':{worker_slot}' if worker_slot else '')
            completed = rotation_completed(rotation_id, rotation['count_failures'])
        proxy['health_providers'] = list(dict.fromkeys(route_key(channel) for channel in channels))
        snapshot = manager.download_snapshot(proxy, completed)
        progress.state.update(proxy_node=snapshot['node'])
        if rotation:
            progress.state['rotation_id'] = rotation_id
            print(f'自动轮换：已计数 {completed} 个视频，每 {rotation["every"]} 个切换节点。', flush=True)
        progress.save(force=True)
        config = {**config, '_resolved_clash_proxy': snapshot['proxy'],
                  '_resolved_clash_node': snapshot.get('node', ''),
                  '_parser_clash_directory': str(manager.directory)}
        from webapp.proxy_health import ProxyHealth, ProxyCoolingDown
        route_health = ProxyHealth(manager.directory)
        eligible_channels = [channel for channel in channels if route_health.eligible(snapshot['node'], route_key(channel))]
        eligible = [provider for provider in methods if any(channel.adapter == provider for channel in eligible_channels)]
        for provider in methods:
            if provider not in eligible:
                event('provider_cooling_down', provider=provider, proxy_node=snapshot['node'])
        if not eligible:
            raise ProxyCoolingDown('当前出口的下载方式均在冷却，稍后重新选择健康线路。')
        methods = eligible
        channels = eligible_channels
        print(f'下载代理节点：{snapshot["node"]}', flush=True)
    event('connection_selected', proxy_mode=config.get('proxy_mode', 'manual'),
          proxy_node=config.get('_resolved_clash_node', ''))
    return config, channels, methods, route_health, snapshot
