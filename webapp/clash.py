"""Project-owned Mihomo process, private subscriptions and public node summaries."""
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from collections import OrderedDict
from copy import deepcopy
from datetime import datetime
import json
import os
from pathlib import Path
import secrets
import re
import socket
import subprocess
import threading
import time
from urllib.error import URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import ProxyHandler, Request, build_opener

import psutil
import yaml

ROOT = Path(__file__).resolve().parents[1]
GROUP = 'DOWNLOAD'
WORKER_GROUP_PREFIX = 'DOWNLOAD_WORKER_'
TEST_URL = 'https://www.gstatic.com/generate_204'
MAX_SUBSCRIPTION = 5 * 1024 * 1024
MAX_WORKERS = 96
MAX_LISTENERS = MAX_WORKERS + 16  # parser routes cannot be changed by transfer workers
_manager_locks = {}
_manager_locks_guard = threading.Lock()
_core_children = {}
_node_cache = OrderedDict()
_node_cache_lock = threading.Lock()


def is_notice(name):
    return bool(re.match(r'^(剩余流量|距离下次重置|套餐到期|到期时间|过期时间|流量重置|官网地址|官方网站|Traffic:|Expire:)', name, re.I))


class ClashError(Exception):
    """Only messages safe to show in the UI may be raised here."""


def atomic_text(path, content):
    temporary = path.with_name(path.name + '.' + secrets.token_hex(6) + '.tmp')
    try:
        temporary.write_text(content, encoding='utf-8')
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def free_port(preferred):
    with socket.socket() as stream:
        try:
            stream.bind(('127.0.0.1', preferred))
        except OSError:
            stream.bind(('127.0.0.1', 0))
        return stream.getsockname()[1]


def parse_subscription(content):
    if not content or len(content) > MAX_SUBSCRIPTION:
        raise ClashError('订阅为空或超过 5 MB。')
    try:
        data = yaml.safe_load(content)
    except (ValueError, yaml.YAMLError, RecursionError):
        raise ClashError('订阅不是有效的 Clash YAML 配置。') from None
    nodes = data.get('proxies') if isinstance(data, dict) else None
    if not isinstance(nodes, list) or not 1 <= len(nodes) <= 1000:
        raise ClashError('订阅中没有可用节点，请使用支持 Clash / Mihomo 的订阅链接。')
    names = set()
    for node in nodes:
        if not isinstance(node, dict):
            raise ClashError('订阅节点格式不正确。')
        name = node.get('name')
        if (not isinstance(name, str) or not name.strip() or len(name) > 256
                or any(ord(char) < 32 for char in name) or name in names
                or name.upper() in ('DIRECT', 'REJECT', 'GLOBAL', GROUP, 'PASS', 'COMPATIBLE')
                or name.upper().startswith(WORKER_GROUP_PREFIX)):
            raise ClashError('订阅节点名称为空、重复或使用了保留名称。')
        if not isinstance(node.get('type'), str) or not isinstance(node.get('server'), str):
            raise ClashError('订阅节点缺少协议或服务器地址。')
        names.add(name)
    return nodes


class ClashManager:
    def __init__(self, directory, binary=None, *, worker_slot=0):
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.binary = Path(binary or ROOT / '.tools' / 'mihomo' / ('mihomo.exe' if os.name == 'nt' else 'mihomo')).resolve()
        if type(worker_slot) is not int or not 0 <= worker_slot < MAX_LISTENERS:
            raise ValueError(f'内部代理槽编号必须为 0 到 {MAX_LISTENERS - 1}。')
        self.worker_slot = worker_slot
        with _manager_locks_guard:
            self.lock, self._lock_state = _manager_locks.setdefault(str(self.directory),
                (threading.RLock(), threading.local()))
        self.direct = build_opener(ProxyHandler({}))
        self.process = None
        self.last_error = ''
        self.testing = {'running': False, 'done': 0, 'total': 0}
        path = self.directory / 'settings.json'
        with self.guard():
            if path.is_file():
                self.config = json.loads(path.read_text(encoding='utf-8'))
            else:
                self.config = {'secret': secrets.token_urlsafe(32), 'proxy_port': free_port(17890),
                               'control_port': free_port(19090), 'selected': '', 'subscription_url': '', 'updated_at': None,
                               'worker_ports': {}, 'worker_nodes': {}}
                while self.config['control_port'] == self.config['proxy_port']:
                    self.config['control_port'] = free_port(0)
                self.save()
        # These fields were added when worker processes moved from one
        # Mihomo process each to listeners in a shared process.  Keep old
        # installations readable and migrate them lazily.
        self.config.setdefault('worker_ports', {})
        self.config.setdefault('worker_nodes', {})
        self.delays = {}
        delay_file = self.directory / 'delays.json'
        if delay_file.is_file():
            try:
                self.delays = json.loads(delay_file.read_text(encoding='utf-8'))
            except ValueError:
                pass

    @property
    def proxy_url(self):
        return f'http://127.0.0.1:{self.proxy_port}'

    @property
    def proxy_port(self):
        if self.worker_slot:
            if str(self.worker_slot) not in self.config['worker_ports']:
                self.start()
                self.reload()
            return int(self.config['worker_ports'][str(self.worker_slot)])
        return int(self.config['proxy_port'])

    @property
    def group(self):
        return f'{WORKER_GROUP_PREFIX}{self.worker_slot}' if self.worker_slot else GROUP

    @contextmanager
    def guard(self):
        """One reentrant lock shared by all views and processes of this core."""
        from web_worker import worker_lock

        with self.lock:
            if getattr(self._lock_state, 'held', False):
                yield
                return
            deadline = time.monotonic() + 60
            while True:
                with worker_lock(self.directory / 'manager.lock') as acquired:
                    if acquired:
                        self._lock_state.held = True
                        try:
                            yield
                        finally:
                            self._lock_state.held = False
                        return
                if time.monotonic() >= deadline:
                    raise ClashError('代理内核管理繁忙，请稍后重试。')
                time.sleep(0.05)

    def save(self):
        atomic_text(self.directory / 'settings.json', json.dumps(self.config, ensure_ascii=False))

    def reload(self):
        # The worker can select another node while Flask keeps this instance alive.
        self.config = json.loads((self.directory / 'settings.json').read_text(encoding='utf-8'))
        self.config.setdefault('worker_ports', {})
        self.config.setdefault('worker_nodes', {})

    def rotation(self):
        return {'enabled': False, 'nodes': [], 'every': 1, 'count_failures': True,
                'id': '', 'start_index': 0, **self.config.get('rotation', {})}

    def configure_rotation(self, data):
        if not isinstance(data, dict) or set(data) != {'enabled', 'nodes', 'every', 'count_failures'}:
            raise ValueError('请提交完整的节点轮换配置。')
        enabled, names, every, count_failures = (data[key] for key in ('enabled', 'nodes', 'every', 'count_failures'))
        if type(enabled) is not bool or type(count_failures) is not bool:
            raise ValueError('轮换开关和失败计数必须为布尔值。')
        if type(every) is not int or not 1 <= every <= 1000:
            raise ValueError('轮换间隔必须是 1 到 1000 之间的整数。')
        if (not isinstance(names, list) or any(not isinstance(name, str) for name in names)
                or len(set(names)) != len(names)):
            raise ValueError('轮换节点必须是无重复的节点名称列表。')
        with self.guard():
            self.reload()
            available = {node['name'] for node in self.nodes() if not is_notice(node['name'])}
            if any(name not in available for name in names):
                raise ValueError('轮换列表包含已移除或无效的节点，请重新选择。')
            if enabled and len(names) < 2:
                raise ValueError('开启自动轮换至少需要选择两个节点。')
            previous = self.rotation()
            if all(previous[key] == data[key] for key in data):
                return
            self.config['rotation'] = {**data, 'id': secrets.token_hex(16),
                                       'start_index': names.index(self.config['selected']) if self.config['selected'] in names else 0}
            self.save()

    def nodes(self):
        path = self.directory / 'nodes.yaml'
        with _node_cache_lock:
            try:
                stat = path.stat()
            except FileNotFoundError:
                _node_cache.pop(path, None)
                return []
            version = (stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size, stat.st_ino)
            cached = _node_cache.get(path)
            if cached is None or cached[0] != version:
                cached = (version, parse_subscription(path.read_bytes()))
                _node_cache[path] = cached
            _node_cache.move_to_end(path)
            while len(_node_cache) > 8:
                _node_cache.popitem(last=False)
            return deepcopy(cached[1])

    def api(self, path, method='GET', data=None, timeout=2):
        url = f'http://127.0.0.1:{self.config["control_port"]}{path}'
        headers = {'Authorization': 'Bearer ' + self.config['secret'], 'Content-Type': 'application/json'}
        request = Request(url, data=json.dumps(data).encode() if data is not None else None,
                          headers=headers, method=method)
        try:
            with self.direct.open(request, timeout=timeout) as response:
                content = response.read(2 * 1024 * 1024)
                return json.loads(content) if content else {}
        except (OSError, ValueError, URLError):
            raise ClashError('代理内核未响应，请启动内核或检查节点是否可用。') from None

    def running(self):
        try:
            return bool(self.api('/version', timeout=0.5).get('version'))
        except ClashError:
            return False

    def runtime(self, nodes, selected=None):
        names = [node['name'] for node in nodes if not is_notice(node['name'])]
        selected = selected or self.config['selected']
        if selected in names:
            names = [selected] + [name for name in names if name != selected]
        groups = [{'name': GROUP, 'type': 'select', 'proxies': names}]
        listeners = []
        # Worker slots use independent listeners and independent select groups
        # inside this same Mihomo process.  A node switch in one slot therefore
        # cannot affect another slot's active HTTP(S) connections.
        for slot, port in sorted((self.config.get('worker_ports') or {}).items(), key=lambda item: int(item[0])):
            try:
                slot_number = int(slot)
                port_number = int(port)
            except (TypeError, ValueError):
                continue
            if slot_number <= 0 or not 1 <= port_number <= 65535:
                continue
            worker_selected = (self.config.get('worker_nodes') or {}).get(str(slot_number), selected)
            worker_names = [worker_selected] + [name for name in names if name != worker_selected] \
                if worker_selected in names else list(names)
            worker_group = f'{WORKER_GROUP_PREFIX}{slot_number}'
            groups.append({'name': worker_group, 'type': 'select', 'proxies': worker_names})
            listeners.append({'name': worker_group.lower(), 'type': 'mixed', 'port': port_number,
                              'listen': '127.0.0.1', 'proxy': worker_group})
        runtime = {'mixed-port': self.config['proxy_port'], 'bind-address': '127.0.0.1',
                'allow-lan': False, 'external-controller': f'127.0.0.1:{self.config["control_port"]}',
                'secret': self.config['secret'], 'mode': 'rule', 'log-level': 'warning',
                'ipv6': False, 'find-process-mode': 'off', 'dns': {'enable': False},
                'profile': {'store-selected': False}, 'proxies': nodes, 'proxy-groups': groups,
                'rules': [f'MATCH,{GROUP}']}
        if listeners:
            runtime['listeners'] = listeners
        return runtime

    def validate_runtime(self, content):
        if not self.binary.is_file():
            raise ClashError('未安装 Mihomo 内核，请将可执行文件放入 .tools/mihomo/。')
        path = self.directory / 'candidate.yaml'
        atomic_text(path, content)
        try:
            result = subprocess.run([str(self.binary), '-t', '-d', str(self.directory), '-f', str(path)],
                                    capture_output=True, timeout=20,
                                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            if result.returncode:
                raise ClashError('订阅未通过内核校验，请确认节点协议受当前 Mihomo 版本支持。')
        except (OSError, subprocess.TimeoutExpired):
            raise ClashError('内核配置校验失败或超时。') from None
        finally:
            path.unlink(missing_ok=True)

    def owned_process(self):
        try:
            saved = json.loads((self.directory / 'process.json').read_text())
            process = psutil.Process(saved['pid'])
            if (abs(process.create_time() - saved['created']) < 0.01
                    and Path(process.exe()).resolve() == self.binary
                    and str(self.directory / 'runtime.yaml') in process.cmdline()):
                return process
        except (OSError, ValueError, KeyError, psutil.Error):
            pass
        return None

    def _root(self):
        return self if not self.worker_slot else ClashManager(self.directory, binary=self.binary)

    def _group_path(self):
        return '/proxies/' + quote(self.group, safe='')

    def _ensure_worker_port(self, slot):
        """Allocate a stable listener port for a worker slot."""
        key = str(int(slot))
        ports = self.config.setdefault('worker_ports', {})
        current = ports.get(key)
        if current:
            return int(current)
        used = {int(self.config.get('proxy_port', 0)), int(self.config.get('control_port', 0))}
        used.update(int(value) for value in ports.values() if str(value).isdigit())
        preferred = 17890 + int(slot)
        while True:
            candidate = free_port(preferred)
            if candidate not in used:
                ports[key] = candidate
                return candidate
            preferred = 0

    def _apply_runtime(self):
        """Write the shared runtime and reload it when the core is alive."""
        nodes = self.nodes()
        runtime_file = self.directory / 'runtime.yaml'
        atomic_text(runtime_file, yaml.safe_dump(self.runtime(nodes), allow_unicode=True, sort_keys=False))
        if self.running():
            self.api('/configs', 'PUT', {'path': str(runtime_file)}, timeout=12)

    def start(self):
        if self.worker_slot:
            return self._root().start()
        with self.guard():
            self.reload()
            running = self.running()
            if (running and self.config.get('listener_layout') == 1
                    and all(str(slot) in self.config.get('worker_ports', {}) for slot in range(1, MAX_LISTENERS))):
                return
            if not self.nodes():
                raise ClashError('请先导入订阅。')
            if not self.binary.is_file():
                raise ClashError('未安装 Mihomo 内核。')
            if not running and self.owned_process():
                raise ClashError('内核进程仍在运行但控制接口不可用，请先停止后再启动。')
            # Prepare all supported slots once.  Changing the download count
            # later never reloads the core or disturbs another active download.
            self._stop_legacy_workers()
            for slot in range(1, MAX_LISTENERS):
                self._ensure_worker_port(slot)
            self.save()
            if running:
                if not self.owned_process():
                    raise ClashError('当前控制端口不是本项目启动的代理内核，未修改配置。')
                self._apply_runtime()
                self.config['listener_layout'] = 1
                self.save()
                return
            listener_ports = [int(value) for value in (self.config.get('worker_ports') or {}).values()
                              if str(value).isdigit()]
            for port in (self.config['proxy_port'], self.config['control_port'], *listener_ports):
                with socket.socket() as stream:
                    # Match Mihomo's listener behavior: recently closed TCP
                    # connections must not prevent restarting the owned core.
                    stream.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    try:
                        stream.bind(('127.0.0.1', port))
                    except OSError:
                        raise ClashError(f'项目代理端口 {port} 被占用，请先关闭占用该端口的程序。') from None
            runtime_file = self.directory / 'runtime.yaml'
            atomic_text(runtime_file, yaml.safe_dump(self.runtime(self.nodes()), allow_unicode=True, sort_keys=False))
            with (self.directory / 'core.log').open('ab') as log:
                self.process = subprocess.Popen([str(self.binary), '-d', str(self.directory), '-f', str(runtime_file)],
                                                stdout=log, stderr=log, stdin=subprocess.DEVNULL,
                                                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            # A short-lived worker view can start the persistent core too.
            # Retain its Popen until stop, even after that view is discarded.
            _core_children[str(self.directory)] = self.process
            process = psutil.Process(self.process.pid)
            atomic_text(self.directory / 'process.json', json.dumps({'pid': process.pid, 'created': process.create_time()}))
            deadline = time.monotonic() + 12
            while time.monotonic() < deadline:
                if self.process.poll() is not None:
                    raise ClashError('代理内核启动失败，请检查端口或订阅配置。')
                if self.running():
                    self.config['listener_layout'] = 1
                    self.save()
                    self.last_error = ''
                    return
                time.sleep(0.15)
            self.stop()
            raise ClashError('代理内核启动超时。')

    def stop(self):
        if self.worker_slot:
            return self._root().stop()
        with self.guard():
            self.reload()
            if self.testing['running']:
                raise ClashError('节点正在测速，请等待测速完成。')
            process = self.owned_process()
            if process:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except psutil.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
            elif self.running():
                raise ClashError('当前端口不是本项目启动的代理进程，未停止。')
            if self.process is not None:
                self.process.poll()
            child = _core_children.pop(str(self.directory), None)
            if child is not None:
                child.poll()
            self._stop_legacy_workers()

    def _stop_legacy_workers(self):
        """Retire only verified worker cores left by the old architecture."""
        directory = self.directory / 'workers'
        if directory.is_dir():
            for child in directory.iterdir():
                if child.is_dir() and child.name.isdigit() and (child / 'settings.json').is_file():
                    manager = ClashManager(child, binary=self.binary)
                    if manager.owned_process():
                        manager.stop()

    def import_subscription(self, url=''):
        with self.guard():
            self.reload()
            if self.testing['running']:
                raise ClashError('节点正在测速，请等待测速完成后更新订阅。')
            if not isinstance(url, str):
                raise ClashError('订阅链接格式不正确。')
            url = url.strip() or self.config['subscription_url']
            parsed = urlsplit(url)
            if (parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username
                    or parsed.password or len(url) > 8192 or any(ord(c) < 32 for c in url)):
                raise ClashError('请输入有效的 HTTP / HTTPS 订阅链接。')
            try:
                request = Request(url, headers={'User-Agent': 'clash.meta', 'Accept': 'application/yaml,text/yaml,*/*'})
                with self.direct.open(request, timeout=25) as response:
                    content = response.read(MAX_SUBSCRIPTION + 1)
            except (OSError, URLError, ValueError):
                raise ClashError('订阅获取失败，请检查链接有效性或网络连接；原订阅保持不变。') from None
            nodes = parse_subscription(content)
            names = [node['name'] for node in nodes if not is_notice(node['name'])]
            if not names:
                raise ClashError('订阅中只有流量或到期提示，没有可选节点。')
            was_running = self.running()
            if was_running:
                self.start()
                self.reload()
            selected = self.config['selected'] if self.config['selected'] in names else names[0]
            runtime = yaml.safe_dump(self.runtime(nodes, selected), allow_unicode=True, sort_keys=False)
            self.validate_runtime(runtime)
            nodes_file = self.directory / 'nodes.yaml'
            runtime_file = self.directory / 'runtime.yaml'
            previous_nodes = nodes_file.read_text(encoding='utf-8') if nodes_file.is_file() else None
            previous_runtime = runtime_file.read_text(encoding='utf-8') if runtime_file.is_file() else None
            if was_running and previous_nodes:
                # Group selections change through the API between reloads.
                # Build rollback content from persisted selections so a failed
                # subscription update restores every worker's latest node.
                previous_runtime = yaml.safe_dump(self.runtime(parse_subscription(previous_nodes.encode('utf-8'))),
                                                  allow_unicode=True, sort_keys=False)
            previous_config = dict(self.config)
            try:
                atomic_text(nodes_file, yaml.safe_dump({'proxies': nodes}, allow_unicode=True, sort_keys=False))
                atomic_text(runtime_file, runtime)
                if was_running:
                    self.api('/configs', 'PUT', {'path': str(runtime_file)}, timeout=12)
                else:
                    self.start()
                self.api('/proxies/' + GROUP, 'PUT', {'name': selected})
                self.config.update(subscription_url=url, selected=selected, subscription_id=secrets.token_hex(16),
                                   updated_at=datetime.now().astimezone().isoformat(timespec='seconds'))
                rotation = self.rotation()
                remaining = [name for name in rotation['nodes'] if name in names]
                if remaining != rotation['nodes']:
                    rotation.update(nodes=remaining, enabled=rotation['enabled'] and len(remaining) >= 2,
                                    id=secrets.token_hex(16),
                                    start_index=remaining.index(selected) if selected in remaining else 0)
                    self.config['rotation'] = rotation
                self.save()
            except (ClashError, OSError):
                if not was_running:
                    self.stop()
                self.config = previous_config
                self.save()
                if previous_nodes is not None:
                    atomic_text(nodes_file, previous_nodes)
                else:
                    nodes_file.unlink(missing_ok=True)
                if previous_runtime is not None:
                    atomic_text(runtime_file, previous_runtime)
                    if was_running:
                        self.api('/configs', 'PUT', {'path': str(runtime_file)}, timeout=12)
                        if self.config['selected']:
                            self.api('/proxies/' + GROUP, 'PUT', {'name': self.config['selected']})
                else:
                    runtime_file.unlink(missing_ok=True)
                raise
            self.delays = {name: delay for name, delay in self.delays.items() if name in names}
            self.last_error = ''
            return len(names)

    def select(self, name, *, automatic=False):
        with self.guard():
            self.reload()
            if name not in {node['name'] for node in self.nodes() if not is_notice(node['name'])}:
                raise ClashError('请选择订阅中的有效节点。')
            self.start()
            self.reload()
            try:
                self.api(self._group_path(), 'PUT', {'name': name})
            except ClashError:
                # PUT is idempotent. A busy freshly started core may have
                # applied it before the short control timeout elapsed.
                if self.api(self._group_path(), timeout=5).get('now') != name:
                    self.api(self._group_path(), 'PUT', {'name': name}, timeout=5)
            if self.worker_slot:
                self.config['worker_nodes'][str(self.worker_slot)] = name
            else:
                self.config['selected'] = name
            rotation = self.rotation()
            if rotation['enabled'] and not automatic and not self.worker_slot:
                rotation.update(id=secrets.token_hex(16),
                                start_index=rotation['nodes'].index(name) if name in rotation['nodes'] else 0)
                self.config['rotation'] = rotation
            self.save()

    def snapshot(self, expected=None):
        with self.guard():
            self.reload()
            self.start()
            self.reload()
            current = self.api(self._group_path()).get('now')
            if not current or (expected is not None and current != expected):
                raise ClashError('代理节点已改变，请重新创建下载任务。')
            snapshot = {'directory': str(self.directory), 'node': current, 'proxy': self.proxy_url,
                        'subscription_id': self.config.get('subscription_id', '')}
            if self.rotation()['enabled']:
                snapshot['rotation'] = self.rotation()
            return snapshot

    def download_snapshot(self, queued, completed):
        """Resolve a queued video's node just before extraction, never during it."""
        rotation = queued.get('rotation')
        if not rotation or not rotation.get('enabled'):
            return self.snapshot(expected=queued['node'])
        with self.guard():
            self.reload()
            if rotation != self.rotation():
                raise ClashError('自动轮换配置已改变，请重新创建下载任务。')
            available = {node['name'] for node in self.nodes() if not is_notice(node['name'])}
            if not set(rotation['nodes']) <= available:
                raise ClashError('轮换节点已被移除，请更新轮换配置后重试。')
            index = (rotation['start_index'] + completed // rotation['every']) % len(rotation['nodes'])
            from webapp.proxy_health import ProxyHealth
            node = ProxyHealth(self.directory).select(rotation['nodes'], index, queued.get('health_providers'))
            self.start()
            if self.api(self._group_path()).get('now') != node:
                self.select(node, automatic=True)
            snapshot = self.snapshot(expected=node)
            snapshot['rotation_completed'] = completed
            return snapshot

    def for_worker(self, slot):
        if type(slot) is not int or not 0 <= slot < MAX_WORKERS:
            raise ValueError(f'下载槽编号必须为 0 到 {MAX_WORKERS - 1}。')
        if slot == 0:
            return self
        return ClashManager(self.directory, binary=self.binary, worker_slot=slot)

    def for_parser(self, slot):
        if type(slot) is not int or not 0 <= slot < 16:
            raise ValueError('解析槽编号必须为 0 到 15。')
        return ClashManager(self.directory, binary=self.binary, worker_slot=MAX_WORKERS + slot)

    def test_delays(self, name=None):
        with self.lock:
            self.start()
            nodes = [node['name'] for node in self.nodes() if not is_notice(node['name'])]
            if name is not None:
                if name not in nodes:
                    raise ClashError('请选择有效节点。')
                nodes = [name]
            if self.testing['running']:
                raise ClashError('已有节点测速正在执行。')
            self.testing = {'running': True, 'done': 0, 'total': len(nodes)}

        def measure(node):
            try:
                result = self.api('/proxies/' + quote(node, safe='') + '/delay?' +
                                  urlencode({'url': TEST_URL, 'timeout': 4000}), timeout=6)
                delay = result.get('delay')
                return node, delay if isinstance(delay, (int, float)) and delay >= 0 else None
            except ClashError:
                return node, None

        def run():
            try:
                with ThreadPoolExecutor(max_workers=6) as pool:
                    for future in as_completed([pool.submit(measure, node) for node in nodes]):
                        node, delay = future.result()
                        with self.lock:
                            self.delays[node] = {'ms': delay, 'checked_at': datetime.now().astimezone().isoformat()}
                            self.testing['done'] += 1
                with self.lock:
                    atomic_text(self.directory / 'delays.json', json.dumps(self.delays, ensure_ascii=False))
            finally:
                with self.lock:
                    self.testing['running'] = False

        threading.Thread(target=run, name='clash-node-delays', daemon=True).start()

    def test_youtube(self):
        snapshot = self.snapshot()
        opener = build_opener(ProxyHandler({'http': snapshot['proxy'], 'https': snapshot['proxy']}))
        start = time.monotonic()
        try:
            with opener.open(Request('https://www.youtube.com/generate_204', headers={'User-Agent': 'Mozilla/5.0'}), timeout=12) as response:
                if response.status not in (200, 204):
                    raise ClashError('当前节点无法连通 YouTube，请选择其他节点。')
                response.read(1024)
        except (OSError, URLError):
            raise ClashError('当前节点无法连通 YouTube，请选择其他节点。') from None
        return {'ok': True, 'node': snapshot['node'], 'ms': round((time.monotonic() - start) * 1000)}

    def status(self):
        with self.lock:
            self.reload()
            running = self.running()
            nodes = self.nodes()
            selected = (self.config['worker_nodes'].get(str(self.worker_slot), self.config['selected'])
                        if self.worker_slot else self.config['selected'])
            from webapp.proxy_health import ProxyHealth
            health = ProxyHealth(self.directory).read()
            if running:
                try:
                    selected = self.api(self._group_path()).get('now', selected)
                except ClashError:
                    running = False
            return {'installed': self.binary.is_file(), 'running': running,
                    'architecture': 'shared_listeners',
                    'subscription_saved': bool(self.config['subscription_url']),
                    'subscription_host': urlsplit(self.config['subscription_url']).hostname or '',
                    'updated_at': self.config['updated_at'], 'selected': selected,
                    'proxy_url': self.proxy_url, 'testing': dict(self.testing), 'error': self.last_error,
                    'rotation': self.rotation(),
                    'notices': [node['name'] for node in nodes if is_notice(node['name'])],
                    'nodes': [{'name': node['name'], 'type': node['type'],
                               'cooling_until': health.get(node['name'], {}).get('until', 0),
                               'delay': self.delays.get(node['name'])} for node in nodes if not is_notice(node['name'])]}
