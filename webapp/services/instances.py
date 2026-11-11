"""Cross-instance readiness and configuration operations."""
from concurrent.futures import ThreadPoolExecutor
from webapp.fleet import DOWNLOAD_ROLES
from webapp.config.settings import DEFAULTS


def preflight_instances(fleet, stage, cancelled, *, rpc):
    nodes = [node for node in fleet.list()['items'] if node['role'] in DOWNLOAD_ROLES and node['online'] and not node['paused']]
    if not nodes:
        raise ValueError('没有在线且启用的下载实例，请先启动或恢复一个实例。')
    def check(node):
        try:
            rpc(fleet, node['id'], '/api/fleet/preflight', method='POST', data={})
            return node, None
        except ValueError as error:
            return node, str(error)
    ready = 0
    with ThreadPoolExecutor(max_workers=min(8, len(nodes))) as pool:
        for node, error in pool.map(check, nodes):
            if cancelled.is_set():
                raise InterruptedError('启动已取消。')
            if error:
                fleet.quarantine(node['id'], error, boot_id=node['boot_id'])
                stage('instance_unavailable:' + node['name'])
            else:
                ready += 1
                stage('instance_ready:' + node['name'])
    if not ready:
        raise ValueError('没有通过检查的下载实例，请在实例与环境中修复并恢复。')
    return None


def update_configuration(fleet, data, *, target, rpc):
    if set(data) - set(DEFAULTS):
        raise ValueError('包含不支持在页面修改的配置项。')
    # Validation and persistence belong to the selected node, using its own config.
    rpc(fleet, target, '/api/config', method='PUT', data=data)
    return dict(message='所选实例配置已保存；后续领取的视频使用新策略，正在执行的视频保持本次配置。')


