"""Short-lived native browser reuse with a lease for every resident browser."""
import atexit
import asyncio
from contextlib import contextmanager
from pathlib import Path
import os
import socket
import subprocess
import tempfile
import threading
import time

from webapp.browser_pool import browser_slot


class NativeBrowser:
    def __init__(self, directory=None, config=None):
        self.process = None
        self.profile = None
        self.address = None
        self.created = time.monotonic()
        self.uses = 0
        self.directory, self.config = directory, config
        self.disk_lease = None
        self.stderr_tail = b''
        self.output_thread = None

    async def start(self, args):
        from webapp import yt5s

        if self.process is not None and self.process.poll() is None:
            return self.address
        self.close()
        self.created = time.monotonic()
        if self.directory:
            from webapp.disk_budget import DiskBudget, MIB
            from types import SimpleNamespace
            self.disk_lease = DiskBudget(self.directory, self.config).lease('browsers', reserve=64 * MIB)
            self.disk_lease.__enter__()
            self.profile = SimpleNamespace(name=str(self.disk_lease.path), cleanup=lambda: None)
        else:
            self.profile = tempfile.TemporaryDirectory(prefix='yt5s-browser-', ignore_cleanup_errors=True)
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            port = listener.getsockname()[1]
        command = [yt5s.browser_executable(getattr(args, 'browser_executable', '')),
                   f'--user-data-dir={self.profile.name}', f'--remote-debugging-port={port}',
                   '--remote-debugging-address=127.0.0.1', '--no-first-run',
                   '--no-default-browser-check', '--start-maximized', '--disk-cache-size=33554432',
                   '--media-cache-size=16777216']
        if os.environ.get('YT5S_PROBE_CONTAINER') == '1' or os.environ.get('YT5S_CONTAINER_BROWSER') == '1':
            command.append('--no-sandbox')
        if getattr(args, 'headless', False):
            command.append('--headless=new')
        if args.proxy:
            command.append('--proxy-server=' + args.proxy)
        command.append('about:blank')
        self.stderr_tail = b''
        self.process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        # Keep only an 8 KiB tail in memory. No browser output files, page dumps,
        # request headers or screenshots accumulate on disk.
        def collect(stream):
            try:
                with stream:
                    while data := stream.read1(4096):
                        if not isinstance(data, bytes):
                            return
                        self.stderr_tail = (self.stderr_tail + data)[-8192:]
            except OSError:
                pass
        self.output_thread = threading.Thread(target=collect, args=(self.process.stdout,), daemon=True)
        self.output_thread.start()
        if self.disk_lease:
            self.disk_lease.child_root_pid = self.process.pid
            self.disk_lease.start_monitor(lambda: self.process.kill() if self.process and self.process.poll() is None else None)
        self.address = f'http://127.0.0.1:{port}'
        opener = yt5s.build_opener(yt5s.ProxyHandler({}))
        deadline = time.monotonic() + 20
        while True:
            try:
                # Do not block the Playwright event loop on a socket timeout.
                def ready():
                    with opener.open(self.address + '/json/version', timeout=1) as response:
                        response.read()
                await asyncio.to_thread(ready)
                return self.address
            except OSError:
                if self.process.poll() is not None or time.monotonic() >= deadline:
                    from webapp.diagnostics import safe_text
                    if self.process.poll() is not None and self.output_thread:
                        self.output_thread.join(timeout=0.2)
                    detail = safe_text(self.stderr_tail.decode('utf-8', errors='replace'))[-3000:]
                    raise RuntimeError('原生浏览器启动失败，请检查显示环境和浏览器安装。'
                                       f' 退出码：{self.process.poll()}；诊断：{detail or "未返回错误输出"}')
                await asyncio.sleep(0.2)

    def close(self):
        process, self.process = self.process, None
        try:
            if process is not None:
                if self.disk_lease:
                    import psutil
                    try:
                        children = psutil.Process(process.pid).children(recursive=True)
                        if process.poll() is None:
                            process.terminate()
                        for child in reversed(children):
                            try:
                                child.kill()
                            except psutil.NoSuchProcess:
                                pass
                        psutil.wait_procs(children, timeout=5)
                    except psutil.NoSuchProcess:
                        pass
                if process.poll() is None:
                    process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
        finally:
            if self.output_thread:
                self.output_thread.join(timeout=0.2)
                self.output_thread = None
            if self.profile is not None:
                self.profile.cleanup()
                self.profile = None
            if self.disk_lease:
                lease, self.disk_lease = self.disk_lease, None
                lease.__exit__(None, None, None)


class BrowserSession:
    """One slot's browser; idle expiry releases both memory and OS capacity."""
    def __init__(self, idle_seconds=5):
        self.lock = threading.RLock()
        self.idle_seconds = idle_seconds
        self.timer = None
        self.owner = None
        self.lease = None
        self.signature = None
        self.generation = 0

    def close(self):
        with self.lock:
            self.generation += 1
            if self.timer:
                self.timer.cancel()
                self.timer = None
            try:
                if self.owner:
                    self.owner.close()
            finally:
                self.owner = None
                if self.lease:
                    self.lease.__exit__(None, None, None)
                    self.lease = None

    @contextmanager
    def use(self, directory, capacity, waiting, slot, signature, *, disk_directory=None, disk_config=None):
        with self.lock:
            self.generation += 1
            if self.timer:
                self.timer.cancel()
                self.timer = None
            limit = capacity() if callable(capacity) else capacity
            key = (str(Path(directory).resolve()), signature, limit)
            if self.owner and (self.signature != key or self.owner.uses >= 20
                               or time.monotonic() - self.owner.created >= 300
                               or self.owner.process is None or self.owner.process.poll() is not None):
                self.close()
            if self.owner is None:
                self.lease = browser_slot(directory, capacity, waiting, preferred=slot)
                try:
                    admitted = self.lease.__enter__()
                except BaseException:
                    self.lease = None
                    raise
                self.owner = NativeBrowser(disk_directory, disk_config) if disk_directory else NativeBrowser()
                self.signature = key
                print(f'yt5s 下载进程 {slot + 1} 使用独立浏览器，浏览器名额 {admitted + 1}。', flush=True)
            try:
                yield self.owner
            except BaseException:
                self.close()
                raise
            else:
                self.owner.uses += 1
                if self.owner.process is None:
                    self.close()
                else:
                    generation = self.generation
                    def expire():
                        with self.lock:
                            if generation == self.generation:
                                self.close()
                    self.timer = threading.Timer(self.idle_seconds, expire)
                    self.timer.daemon = True
                    self.timer.start()


_sessions = {}
_sessions_lock = threading.Lock()


def session(directory, slot):
    key = (str(Path(directory).resolve()), slot)
    with _sessions_lock:
        if key not in _sessions:
            _sessions[key] = BrowserSession()
        return _sessions[key]


def close_all():
    with _sessions_lock:
        for value in _sessions.values():
            value.close()
        _sessions.clear()


atexit.register(close_all)
