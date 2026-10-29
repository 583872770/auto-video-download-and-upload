"""Instance-local Bergung parser pool, authenticated JSON over loopback.

Only the resolver service owns these browsers. Download workers retain task
leases. A disconnected/killed client cancels its page before a slot is reused.
Resolved media addresses only pass through memory and the local socket.
"""
import asyncio
from contextlib import contextmanager
import hmac
import json
import os
from pathlib import Path
import secrets
import select
import socket
import socketserver
import threading
import time

import psutil

from webapp.throughput import PipelineBusy, _atomic, runtime_limits

MAX_MESSAGE = 2 * 1024 * 1024
CONFIG_KEYS = {'proxy_mode', 'proxy', '_resolved_clash_proxy', '_resolved_clash_node',
               '_parser_clash_directory', 'browser_executable', 'bergung_quality', 'bergung_timeout',
               '_bergung_site_url', '_bergung_api_url', '_channel_id', 'yt5s_browser_count',
               'throughput_mode', 'worker_count', 'bandwidth_mbps', 'disk_min_free_gb',
               'disk_resume_free_gb'}
CONFIG_KEYS |= {'_cobalt_verification', '_resolver_deadline'}


def disconnected(stream):
    try:
        if not select.select([stream], [], [], 0)[0]:
            return False
        return stream.recv(1, socket.MSG_PEEK) == b''
    except OSError:
        return True


async def cancellable(work, check):
    if check is None:
        return await work
    async def watch():
        while not check():
            await asyncio.sleep(.25)
        raise asyncio.CancelledError()
    task, guard = asyncio.create_task(work), asyncio.create_task(watch())
    try:
        done, _ = await asyncio.wait((task, guard), return_when=asyncio.FIRST_COMPLETED)
        if guard in done:
            raise asyncio.CancelledError()
        return await task
    finally:
        task.cancel()
        guard.cancel()
        await asyncio.gather(task, guard, return_exceptions=True)


class ParserBroker:
    def __init__(self, directory, settings):
        self.directory = Path(directory)
        self.settings = settings
        self.stop = threading.Event()
        self.condition = threading.Condition()
        self.busy = set()
        self.sessions = {}
        self.token = secrets.token_urlsafe(40)
        self.server = self.thread = self.lock = None

    def capacity(self):
        config = self.settings()
        return runtime_limits(config, self.directory)['browsers']

    @contextmanager
    def checkout(self, cancelled, config, waiting):
        from webapp.yt5s_browser import BrowserSession
        key = (config.get('_resolved_clash_node') or config.get('proxy', ''),
               config.get('_bergung_site_url', ''), config.get('_bergung_api_url', ''))
        slot = None
        deadline = time.monotonic() + (90 if config.get('_resolver_deadline') else 300)
        try:
            while slot is None:
                if self.stop.is_set() or cancelled():
                    raise asyncio.CancelledError()
                if time.monotonic() >= deadline:
                    raise PipelineBusy('解析浏览器额度繁忙，稍后重新准备。')
                with self.condition:
                    capacity = self.capacity()
                    # Retire idle browsers above a newly reduced ceiling.
                    for index, (_, session) in list(self.sessions.items()):
                        if index >= capacity and index not in self.busy:
                            session.close()
                            self.sessions.pop(index)
                    choices = [i for i in range(capacity) if i not in self.busy]
                    choices.sort(key=lambda i: (self.sessions.get(i, (None,))[0] != key, i))
                    if choices:
                        slot = choices[0]
                        self.busy.add(slot)
                        if slot not in self.sessions:
                            self.sessions[slot] = (key, BrowserSession(idle_seconds=30))
                        else:
                            self.sessions[slot] = (key, self.sessions[slot][1])
                    else:
                        waiting('waiting_browser')
                        self.condition.wait(.5)
            yield slot, self.sessions[slot][1]
        finally:
            if slot is not None:
                with self.condition:
                    self.busy.discard(slot)
                    self.condition.notify_all()

    def start(self):
        from webapp.disk_budget import file_lock
        self.lock = file_lock(self.directory / 'parser-broker.lock')
        if not self.lock.__enter__():
            self.lock.__exit__(None, None, None)
            self.lock = None
            return
        broker = self

        class Server(socketserver.ThreadingTCPServer):
            allow_reuse_address = True
            daemon_threads = True
            request_queue_size = 96

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                self.connection.settimeout(10)
                line = self.rfile.readline(65537)
                if len(line) > 65536:
                    return
                try:
                    message = json.loads(line)
                    if not hmac.compare_digest(str(message.get('token', '')), broker.token):
                        return
                    pid, created = int(message['pid']), float(message['created'])
                    if abs(psutil.Process(pid).create_time() - created) > .01:
                        return
                    config = {key: value for key, value in message['config'].items() if key in CONFIG_KEYS}
                    # Disk budgets and filesystem locations come from this
                    # instance, never from a request to the local protocol.
                    local = broker.settings()
                    config.update({key: value for key, value in local.items() if key.startswith('disk_')})
                    config.update(_parser_local=True, _disk_state=str(broker.directory))
                    def cancelled():
                        return broker.stop.is_set() or disconnected(self.connection) or not psutil.pid_exists(pid)
                    config['_parser_cancel_check'] = cancelled
                    last_stage = [None, 0]
                    def send(value):
                        data = (json.dumps(value, ensure_ascii=False) + '\n').encode()
                        if len(data) > MAX_MESSAGE:
                            raise ValueError('parser response too large')
                        self.wfile.write(data)
                        self.wfile.flush()
                    def stage(value):
                        if value != last_stage[0] or time.monotonic() - last_stage[1] > 5:
                            send({'stage': value})
                            last_stage[:] = [value, time.monotonic()]
                    with broker.checkout(cancelled, config, stage) as (slot, session):
                        config['_parser_session'] = session
                        if config.get('proxy_mode') == 'clash' and config.get('_parser_clash_directory'):
                            from webapp.clash import ClashManager
                            manager = ClashManager(config['_parser_clash_directory']).for_parser(slot)
                            manager.select(config['_resolved_clash_node'], automatic=True)
                            config['_resolved_clash_proxy'] = manager.snapshot(expected=config['_resolved_clash_node'])['proxy']
                        from webapp.bergung import resolve
                        info = resolve(message['url'], config, Path(message['base']), broker.directory,
                                       stage, worker_slot=slot, browser_capacity=broker.capacity)
                        if not cancelled():
                            send({'result': info})
                except asyncio.CancelledError:
                    pass
                except Exception as error:
                    from webapp.diagnostics import safe_text
                    from webapp.proxy_health import connection_failure
                    try:
                        send({'error': safe_text(str(error))[:2000], 'type': type(error).__name__,
                              'retryable': bool(getattr(error, 'retryable', False)),
                              'failure_scope': getattr(error, 'failure_scope', 'channel'),
                              'http_status': getattr(error, 'http_status', None),
                              'connection_failure': connection_failure(error),
                              'errno': getattr(error, 'errno', None),
                              'retry_after': getattr(error, 'retry_after', None)})
                    except (OSError, UnboundLocalError):
                        pass

        try:
            self.server = Server(('127.0.0.1', 0), Handler)
            _atomic(self.directory / 'parser-broker.json', {'pid': os.getpid(), 'token': self.token,
                    'port': self.server.server_address[1], 'created': psutil.Process().create_time()})
            self.thread = threading.Thread(target=self.server.serve_forever, name='video-parser-broker', daemon=True)
            self.thread.start()
        except BaseException:
            self.close()
            raise

    def close(self):
        self.stop.set()
        with self.condition:
            self.condition.notify_all()
        if self.server:
            self.server.shutdown()
            self.server.server_close()
        # Cancellation closes active pages before their BrowserSession lock
        # becomes available. Do not close a browser being used by another task.
        for _, session in list(self.sessions.values()):
            session.close()
        if self.lock:
            (self.directory / 'parser-broker.json').unlink(missing_ok=True)
            self.lock.__exit__(None, None, None)
            self.lock = None


def resolve_remote(url, config, base, directory, on_stage=None):
    """Return None only if no broker exists; never duplicate an in-flight parse."""
    path = Path(directory) / 'parser-broker.json'
    try:
        address = json.loads(path.read_text(encoding='utf-8'))
        owner = psutil.Process(address['pid'])
        if abs(owner.create_time() - address['created']) > .01:
            return None
        connection = socket.create_connection(('127.0.0.1', address['port']), timeout=3)
    except (OSError, ValueError, KeyError, psutil.Error):
        return None
    with connection:
        connection.settimeout(1)
        request = {'token': address['token'], 'url': url, 'base': str(base),
                   'pid': os.getpid(), 'created': psutil.Process().create_time(),
                   'config': {key: value for key, value in config.items() if key in CONFIG_KEYS}}
        connection.sendall((json.dumps(request) + '\n').encode())
        data = b''
        deadline = time.monotonic() + (int(config['_resolver_deadline']) + 110
            if config.get('_resolver_deadline') else int(config.get('bergung_timeout', 180)) + 360)
        while time.monotonic() < deadline:
            try:
                part = connection.recv(65536)
            except socket.timeout:
                continue
            if not part:
                raise PipelineBusy('解析服务已重启，稍后重新准备下载。')
            data += part
            if len(data) > MAX_MESSAGE:
                raise RuntimeError('解析服务响应超出大小限制。')
            while b'\n' in data:
                line, data = data.split(b'\n', 1)
                value = json.loads(line)
                if 'stage' in value and on_stage:
                    on_stage(value['stage'])
                if 'result' in value:
                    return value['result']
                if 'error' in value:
                    from webapp.bergung import BergungError
                    from webapp.yt5s import BrowserEnvironmentRequired, WebsiteTimeoutError
                    if value['type'] == 'BrowserEnvironmentRequired':
                        raise BrowserEnvironmentRequired(value['error'])
                    if value['type'] == 'WebsiteTimeoutError':
                        raise WebsiteTimeoutError(value['error'])
                    from webapp.disk_budget import DiskPressure
                    from webapp.environment import DownloadEnvironmentRequired
                    if value['type'] in ('DiskPressure', 'DiskCoordinationBusy'):
                        raise DiskPressure(value['error'])
                    if value['type'] == 'DownloadEnvironmentRequired':
                        raise DownloadEnvironmentRequired(value['error'])
                    if value['type'] == 'PipelineBusy':
                        raise PipelineBusy(value['error'])
                    if value.get('errno') in (5, 13, 28, 30, 122):
                        raise OSError(value['errno'], value['error'])
                    error = BergungError(value['error'], retryable=value.get('retryable', False),
                                         http_status=value.get('http_status'), retry_after=value.get('retry_after'),
                                         failure_scope=value.get('failure_scope', 'channel'))
                    error.connection_failure = value.get('connection_failure', False)
                    raise error
        raise PipelineBusy('解析服务等待超时，稍后重新准备下载。')
