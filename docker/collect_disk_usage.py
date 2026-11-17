"""Host-side read-only collection; only the aggregate JSON is shared with the app.

Run with the system Python on the Docker host, for example from a five-minute
systemd timer. No third-party modules and no Docker socket in the Web container.
"""
import argparse
import http.client
import json
import os
from pathlib import Path
import socket
import subprocess
import time


def command(args, *, timeout=60):
    result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, timeout=timeout)
    if result.returncode:
        # Do not include stderr or arguments: MySQL credentials are child-only.
        raise RuntimeError('采集命令执行失败')
    return result.stdout


def inspect(name):
    return json.loads(command(['docker', 'inspect', name], timeout=15))[0]


def allocated(path):
    return int(command(['du', '-x', '-s', '-B1', str(path)], timeout=90).split()[0])


class DockerHTTP(http.client.HTTPConnection):
    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect('/var/run/docker.sock')


def image_usage():
    connection = DockerHTTP('localhost', timeout=30)
    try:
        connection.request('GET', '/system/df')
        response = connection.getresponse()
        if response.status != 200:
            raise RuntimeError('无法采集 Docker 镜像用量')
        result = json.loads(response.read())
    finally:
        connection.close()
    # LayersSize counts shared layers once; virtual image sizes must not be added.
    total = result['LayersSize']
    reclaimable = sum(max(0, i['Size']-max(0, i.get('SharedSize', 0)))
                      for i in result.get('Images', []) if i.get('Containers') == 0)
    return dict(used_bytes=total, reclaimable_bytes=min(total, reclaimable),
                note='共享文件层只计一次；可回收量为未使用镜像独占层的估算。')


def mysql_usage(node):
    mount = next(m for m in node['Mounts'] if m['Destination'] == '/var/lib/mysql')
    root = Path(mount['Source'])
    device = str(root.stat().st_dev)
    binlogs = sum(p.stat().st_blocks*512 for p in root.glob('binlog.*')
                  if p.is_file() and p.name != 'binlog.index')
    total = allocated(root)
    retention = None
    try:
        output = command(['docker', 'exec', node['Id'], 'sh', '-c',
            'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" exec mysql -u root --batch --skip-column-names '
            '-e "SELECT @@binlog_expire_logs_seconds"'], timeout=15)
        retention = round(int(output.strip())/86400, 2)
    except (KeyError, ValueError, RuntimeError, subprocess.TimeoutExpired):
        pass
    return [dict(key='mysql_binlog', used_bytes=binlogs, device=device, retention_days=retention,
                 note='数据库变更日志；清理使用 MySQL 正规命令，不能直接删除数据文件。'),
            dict(key='mysql_data', used_bytes=max(0, total-binlogs), device=device,
                 note='数据库业务文件、索引与内部文件，已排除 binlog。')]


def collect(app_container, deployments):
    app = inspect(app_container)
    mount = next(m for m in app['Mounts'] if m['Destination'] == '/app/.cache/web')
    state = Path(mount['Source']).resolve()
    if not state.is_dir():
        raise RuntimeError('应用状态目录不存在')
    result = dict(sampled_at=time.time(), sources=[])
    try:
        info = json.loads(command(['docker', 'info', '--format', '{{json .DockerRootDir}}'], timeout=15))
        value = image_usage()
        result['sources'].append(dict(key='docker_images', device=str(Path(info).stat().st_dev), **value))
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired):
        result['sources'].append(dict(key='docker_images', used_bytes=None, note='Docker 用量采集失败。'))
    try:
        folder = Path(deployments).resolve()
        result['sources'].append(dict(key='deployments', used_bytes=allocated(folder),
            device=str(folder.stat().st_dev), note='包括当前与历史部署文件；当前版本文件需保留。'))
    except (OSError, RuntimeError, subprocess.TimeoutExpired):
        result['sources'].append(dict(key='deployments', used_bytes=None, note='部署目录用量采集失败。'))
    try:
        # Only consider a database container on this app's Docker networks.
        networks = set(app['NetworkSettings']['Networks'])
        environment = dict(item.split('=', 1) for item in app['Config'].get('Env', []) if '=' in item)
        database_host = environment.get('MYSQL_HOST')
        candidates = []
        for identity in command(['docker', 'ps', '-q'], timeout=15).splitlines():
            node = inspect(identity)
            common = networks & set(node['NetworkSettings']['Networks'])
            names = {node.get('Name', '').lstrip('/'), node.get('Config', {}).get('Hostname', '')}
            for network in common:
                address = node['NetworkSettings']['Networks'][network]
                names.update(address.get('Aliases') or [])
                names.add(address.get('IPAddress'))
            if database_host and database_host in names and common and any(
                    m['Destination'] == '/var/lib/mysql' for m in node['Mounts']):
                candidates.append(node)
        if len(candidates) == 1:
            result['sources'].extend(mysql_usage(candidates[0]))
        else:
            result['sources'].append(dict(key='mysql_binlog', used_bytes=None,
                note='未唯一识别本机数据库；远程数据库需在所属主机采集。'))
    except (OSError, ValueError, StopIteration, RuntimeError, subprocess.TimeoutExpired):
        result['sources'].append(dict(key='mysql_binlog', used_bytes=None, note='数据库用量采集失败。'))
    return state, result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--app-container', default='youtube-downloader-local-app-1')
    parser.add_argument('--deployments-directory', default='/root/deployments')
    args = parser.parse_args()
    state, result = collect(args.app_container, args.deployments_directory)
    target = state/'host-disk-usage.json'
    temporary = state/'host-disk-usage.json.tmp'
    with temporary.open('w', encoding='utf-8') as stream:
        json.dump(result, stream, ensure_ascii=False)
    os.chmod(temporary, 0o644)
    temporary.replace(target)
    print(json.dumps({'sampled_at': result['sampled_at'], 'sources': len(result['sources'])}))


if __name__ == '__main__':
    main()
