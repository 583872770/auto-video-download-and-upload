"""A single independent YouTube login window, separate from the download queue."""
import asyncio
import json
from pathlib import Path
import re
import tempfile
import threading
import time
import unicodedata
import uuid

from playwright.async_api import Error as BrowserError
from werkzeug.exceptions import Conflict, NotFound

from scripts.login_youtube import login, LoginCancelled
from web_worker import worker_lock
from webapp.tasks import now

ACTIVE = ('starting', 'waiting', 'saving', 'committing', 'cancelling')


class LoginBusy(ValueError):
    pass


def browser_cookie_name(account_name):
    """Keep the account label and a timestamp within the profile's name limit."""
    label = account_name if isinstance(account_name, str) else ''
    label = ' '.join(''.join(char for char in label
                            if not unicodedata.category(char).startswith('C')).split())
    stamp = time.strftime('%Y-%m-%d %H:%M:%S')
    suffix = ' · ' + stamp
    return label[:80 - len(suffix)].rstrip() + suffix if label else '浏览器登录 ' + stamp


def browser_failure_message(error, stage):
    """Classify errors without exposing browser arguments or proxy credentials."""
    detail = str(error).lower()
    if "executable doesn't exist" in detail or 'executable does not exist' in detail:
        return '未找到 Chrome/Chromium。Docker 部署请重新构建并创建包含浏览器的容器（up -d --build）。'
    if 'missing x server' in detail or '$display' in detail or 'cannot open display' in detail:
        return '浏览器缺少显示环境。Docker 部署请检查 Xvfb 是否启动及 DISPLAY 配置，通过 noVNC 远程桌面查看登录窗口。'
    if 'error while loading shared libraries' in detail or 'host system is missing dependencies' in detail:
        return '浏览器缺少系统依赖。Docker 部署请重新构建包含 Chromium 和 Xvfb 的镜像。'
    network_code = re.search(r'\bnet::(ERR_[A-Z0-9_]+)\b', str(error))
    if network_code:
        return f'浏览器访问 YouTube 失败（{network_code[1]}），请检查当前下载连接和所选代理节点。'
    if 'timeout' in detail:
        return '浏览器操作超时，请检查当前下载连接和所选代理节点能否访问 YouTube，并通过服务器远程桌面查看窗口。'
    if stage == 'starting':
        return '独立浏览器启动或打开 YouTube 失败，请检查 Chromium、Xvfb 显示环境及所选代理；Docker 登录窗口需通过 noVNC 远程桌面查看。'
    return '登录浏览器已关闭或连接中断，请重新打开登录窗口；Docker 登录窗口需通过 noVNC 远程桌面查看。'


class YouTubeLogin:
    def __init__(self, directory, install):
        self.directory = Path(directory) / 'youtube-login'
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / 'status.json'
        self.lock = threading.RLock()
        self.install = install
        self.thread = None

    def _read(self):
        if self.path.is_file():
            return json.loads(self.path.read_text(encoding='utf-8'))
        return {'state': 'idle', 'message': '尚未启动独立浏览器登录。'}

    def _write(self, state):
        events = state.setdefault('events', [])
        event = {'state': state['state'], 'message': state.get('message', '')}
        if not events or any(events[-1].get(key) != value for key, value in event.items()):
            events.append({**event, 'at': now()})
            del events[:-24]
        temporary = self.directory / (uuid.uuid4().hex + '.tmp')
        try:
            temporary.write_text(json.dumps(state, ensure_ascii=False), encoding='utf-8')
            temporary.replace(self.path)
        finally:
            temporary.unlink(missing_ok=True)

    def status(self):
        with self.lock:
            state = self._read()
            if state['state'] in ACTIVE:
                # The OS releases this lock on process exit; never trust a stale status.
                with worker_lock(self.directory / 'session.lock') as acquired:
                    if acquired:
                        state.update(state='interrupted', message='登录服务已退出，请重新打开登录窗口。', finished_at=now())
                        self._write(state)
            return {**state, 'active': state['state'] in ACTIVE}

    def start(self, config, connection, *, account=None, credentials=None):
        with self.lock:
            guard = worker_lock(self.directory / 'session.lock')
            if not guard.__enter__():
                guard.__exit__(None, None, None)
                raise LoginBusy('已有登录窗口，请完成或取消后再试。')
            try:
                state = {'id': uuid.uuid4().hex, 'state': 'starting', 'message': '正在启动独立浏览器…',
                         'connection': connection, 'started_at': now(), 'finished_at': None}
                if account:
                    state['account'] = account
                self._write(state)
                self.thread = threading.Thread(target=self._run, args=(config, state, guard, credentials),
                                               name='youtube-login', daemon=True)
                self.thread.start()
            except BaseException:
                guard.__exit__(None, None, None)
                raise
            return {**state, 'active': True}

    def cancel(self):
        with self.lock:
            state = self.status()
            if state['active'] and state['state'] != 'committing':
                (self.directory / (state['id'] + '.cancel')).touch()
                state.update(state='cancelling', message='正在取消登录并关闭独立浏览器…')
                state.pop('active', None)
                self._write(state)
            return self.status()

    def _run(self, config, state, guard, credentials=None):
        cancel_file = self.directory / (state['id'] + '.cancel')

        class Cancellation:
            def is_set(self):
                return cancel_file.exists()

        def report(stage, message):
            with self.lock:
                if not cancel_file.exists():
                    state.update(state=stage, message=message)
                    self._write(state)

        try:
            with tempfile.TemporaryDirectory(prefix='cookies-', dir=self.directory) as staging:
                output = Path(staging) / 'youtube.txt'
                options = {}
                if state.get('account'):
                    options.update(credentials=credentials, expected_email=state['account']['email'])
                result = asyncio.run(login(config, output, on_status=report, cancelled=Cancellation(), **options))
                credentials = None
                with self.lock:
                    if cancel_file.exists():
                        raise LoginCancelled('登录已取消，已有 Cookie 保持不变。')
                    state.update(state='committing', message='正在保存登录结果…')
                    self._write(state)
                account_name = result.get('account_name') if isinstance(result, dict) else None
                options = {'account': state['account']} if state.get('account') else {}
                message = self.install(output.read_bytes(), account_name=account_name, **options)
                state.update(state='succeeded', message=message or 'YouTube 登录成功，Cookie 已保存，后续下载将自动使用。')
        except LoginCancelled as error:
            state.update(state='cancelled', message=str(error))
        except BrowserError as error:
            # Playwright errors may contain proxy URLs or browser process arguments.
            message = browser_failure_message(error, state['state'])
            state.update(state='failed', message=message)
        except (ValueError, RuntimeError) as error:
            state.update(state='failed', message=str(error))
        except (Conflict, NotFound) as error:
            state.update(state='failed', message=error.description)
        except Exception:
            state.update(state='failed', message='登录结果保存失败，请检查配置文件、数据库与 Cookie 目录后重试。')
        finally:
            credentials = None
            with self.lock:
                state['finished_at'] = now()
                try:
                    self._write(state)
                    cancel_file.unlink(missing_ok=True)
                finally:
                    guard.__exit__(None, None, None)
