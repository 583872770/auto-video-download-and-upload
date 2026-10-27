"""Structured yt-dlp progress persisted independently of terminal output."""
import math
import re
import time
from urllib.parse import urlsplit, urlunsplit


class DownloadLeaseLost(RuntimeError):
    """Do not start another provider after ownership of a task was lost."""


def initial_progress(config, policy_version=None):
    """Publish this attempt's selected providers as part of the claim itself."""
    from webapp.settings import download_methods
    methods = download_methods(config)
    return dict(stage='preparing_workspace', percent=0, downloaded_bytes=0,
                total_bytes=None, speed=None, eta=None, title='', format='',
                download_method=methods[0], selected_download_methods=methods,
                policy_version=policy_version, storage_mode=config.get('storage_mode', 'local'),
                stage_started_at=time.time(), elapsed=0, stage_seconds={})


def clean_download_error(message):
    """Keep the extractor reason, excluding terminal escapes and signed URL data."""
    message = re.sub(r'\x1b\[[0-9;]*m', '', str(message))

    def redact_url(match):
        try:
            url = urlsplit(match[0])
            return urlunsplit((url.scheme, url.netloc.rsplit('@', 1)[-1], url.path,
                               '…' if url.query else '', ''))
        except ValueError:
            return '[URL]'

    message = re.sub(r'https?://[^\s<>\"\']+', redact_url, message)
    return ' '.join(message.split())[:1000]


class DownloadFailure:
    """Retain yt-dlp's cause even when download() only returns a failure code."""
    def __init__(self):
        self.cookie_rejected = False
        self.errors = []

    def warning(self, message):
        if 'account cookies are no longer valid' in str(message).lower():
            self.cookie_rejected = True

    def error(self, message):
        self.errors = [*self.errors[-2:], clean_download_error(message)]

    def message(self, exception=None):
        reason = self.errors[-1] if self.errors else clean_download_error(exception or '')
        bot_check = 'confirm' in reason.lower() and 'bot' in reason.lower()
        if self.cookie_rejected:
            return ('YouTube 提示登录 Cookie 已失效或被轮换' + ('，并要求登录验证。' if bot_check else '。')
                    + '请在浏览器重新登录 YouTube，导出并上传新的 Cookie 后重试。'
                    + ('若仍要求验证，请检查所选代理节点在浏览器中能否正常播放该视频。' if bot_check else ''))
        if bot_check:
            return ('YouTube 要求登录验证，当前 Cookie 或代理节点未通过验证。'
                    '请使用当前节点在浏览器打开视频，完成登录验证后重新导出并上传 Cookie；仍失败可更换节点。')
        return f'下载失败：{reason}' if reason else '下载失败，下载器未返回具体原因，请查看日志。'


def number(value):
    return value if isinstance(value, (int, float)) and math.isfinite(value) and value >= 0 else None


class DownloadProgress:
    def __init__(self, write):
        self.write = write
        self.last_write = 0
        self.streams = {}
        self.formats = []
        self.started_at = time.time()
        self.stage_started_at = self.started_at
        self.current_stage = None
        self.stage_seconds = {}
        self.state = {'stage': 'extracting', 'percent': 0, 'downloaded_bytes': 0,
                      'total_bytes': None, 'speed': None, 'eta': None, 'title': '', 'format': ''}
        self.save(force=True)

    def begin_video(self, info):
        self.formats = info.get('requested_formats') or []
        self.streams = {}
        self.state.update(stage='extracting', title=str(info.get('title') or ''), percent=0,
                          downloaded_bytes=0, total_bytes=None, speed=None, eta=None, format='')
        self.save(force=True)

    def save(self, force=False):
        stamp = time.time()
        stage = (self.state.get('download_method'), self.state.get('stage'))
        if stage != self.current_stage:
            if self.current_stage:
                name = self.current_stage[1]
                self.stage_seconds[name] = self.stage_seconds.get(name, 0) + max(0, stamp - self.stage_started_at)
            self.stage_started_at, self.current_stage = stamp, stage
        self.state.update(stage_started_at=self.stage_started_at, elapsed=round(stamp - self.started_at, 1),
                          stage_seconds={**self.stage_seconds, self.state['stage']:
                              round(self.stage_seconds.get(self.state['stage'], 0) + stamp - self.stage_started_at, 1)})
        if force or time.monotonic() - self.last_write >= 1.5:
            self.write(dict(self.state))
            self.last_write = time.monotonic()

    def hook(self, event):
        previous_stage = self.state['stage']
        info = event.get('info_dict') or {}
        # Subtitle downloads have their own progress hooks; keep the video meter separate.
        if info.get('ext') in ('vtt', 'srt', 'ass', 'ttml', 'srv1', 'srv2', 'srv3', 'json3'):
            return
        filename = event.get('filename') or info.get('format_id') or 'video'
        previous = self.streams.get(filename, {})
        downloaded = number(event.get('downloaded_bytes')) or previous.get('downloaded', 0)
        total = number(event.get('total_bytes')) or number(event.get('total_bytes_estimate'))
        finished = event.get('status') == 'finished'
        self.streams[filename] = {'downloaded': downloaded, 'total': total, 'finished': finished}
        formats = info.get('requested_formats') or self.formats
        count = max(len(formats), len(self.streams), 1)
        fraction = min(downloaded / total, 1) if total else 0
        completed = sum(stream['finished'] for stream in self.streams.values())
        percent = 100 * (completed + (0 if finished else fraction)) / count
        estimates = [number(fmt.get('filesize')) or number(fmt.get('filesize_approx')) for fmt in formats]
        all_total = sum(estimates) if estimates and all(size is not None for size in estimates) else None
        if all_total is None and count == 1:
            all_total = total
        if all_total is None and len(self.streams) == count and all(stream['total'] for stream in self.streams.values()):
            all_total = sum(stream['total'] for stream in self.streams.values())
        format_name = '音频' if info.get('vcodec') == 'none' else '视频'
        self.state.update(stage='processing' if finished else 'downloading',
                          percent=round(min(percent, 100), 1) if total or finished else None,
                          downloaded_bytes=sum(stream['downloaded'] for stream in self.streams.values()),
                          total_bytes=all_total, speed=None if finished else number(event.get('speed')),
                          eta=None if finished else number(event.get('eta')), title=str(info.get('title') or self.state['title']),
                          format=f'{format_name} · {info.get("format_id", "")}')
        self.save(force=finished or previous_stage != self.state['stage'])

    def postprocess(self, event):
        info = event.get('info_dict') or {}
        self.state.update(stage='processing', speed=None, eta=None,
                          title=str(info.get('title') or self.state['title']))
        self.save(force=True)

