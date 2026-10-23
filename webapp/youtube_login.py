"""A single independent YouTube login window, separate from the download queue."""
import asyncio
import json
from pathlib import Path
import tempfile
import threading
import uuid

from playwright.async_api import Error as BrowserError

from scripts.login_youtube import login, LoginCancelled
from web_worker import worker_lock
from webapp.tasks import now

ACTIVE = ('starting', 'waiting', 'saving', 'committing', 'cancelling')


class LoginBusy(ValueError):
    pass


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

    def start(self, config, connection):
        with self.lock:
            guard = worker_lock(self.directory / 'session.lock')
            if not guard.__enter__():
                guard.__exit__(None, None, None)
                raise LoginBusy('已有登录窗口，请完成或取消后再试。')
            try:
                state = {'id': uuid.uuid4().hex, 'state': 'starting', 'message': '正在启动独立浏览器…',
                         'connection': connection, 'started_at': now(), 'finished_at': None}
                self._write(state)
                self.thread = threading.Thread(target=self._run, args=(config, state, guard),
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

    def _run(self, config, state, guard):
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
                asyncio.run(login(config, output, on_status=report, cancelled=Cancellation()))
                with self.lock:
                    if cancel_file.exists():
                        raise LoginCancelled('登录已取消，已有 Cookie 保持不变。')
                    state.update(state='committing', message='正在保存登录结果…')
                    self._write(state)
                self.install(output.read_bytes())
                state.update(state='succeeded', message='YouTube 登录成功，Cookie 已保存，后续下载将自动使用。')
        except LoginCancelled as error:
            state.update(state='cancelled', message=str(error))
        except BrowserError:
            # Playwright errors may contain proxy URLs or browser process arguments.
            state.update(state='failed', message='浏览器无法启动、访问 YouTube 或已被关闭。请检查浏览器安装和所选代理后重试；也可上传 Cookie 文件。')
        except (ValueError, RuntimeError) as error:
            state.update(state='failed', message=str(error))
        except Exception:
            state.update(state='failed', message='登录结果保存失败，请检查配置文件、数据库与 Cookie 目录后重试。')
        finally:
            with self.lock:
                state['finished_at'] = now()
                try:
                    self._write(state)
                    cancel_file.unlink(missing_ok=True)
                finally:
                    guard.__exit__(None, None, None)
