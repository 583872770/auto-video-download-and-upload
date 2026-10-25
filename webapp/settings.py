"""Validate settings used by the YouTube download service."""
import re
from pathlib import Path
import tempfile
from urllib.parse import urlsplit

YOUTUBE_PREFIX = 'https://www.youtube.com/watch?v='

import yaml

DEFAULTS = {
    'path': 'videos', 'dir': '', 'url': '', 'proxy': '', 'proxy_mode': 'manual', 'youtube_cookie': '',
    'browser_executable': '', 'download_format': '', 'js_runtimes': None,
    'worker_count': 1, 'download_attempts': 3, 'min_free_gb': 2,
    'log_retention_days': 0, 'max_log_mb': 10, 'shard_downloads': True, 'storage_mode': 'local',
    'download_method': 'yt_dlp', 'yt5s_quality': '360', 'yt5s_browser_count': 2,
    'fragment_concurrency': 4,
    'downmp3_quality': '720', 'downmp3_timeout': 300,
}
DOWNLOAD_FIELDS = {'path', 'dir', 'url', 'proxy', 'proxy_mode', 'youtube_cookie', 'worker_count',
                   'download_attempts', 'min_free_gb', 'log_retention_days', 'max_log_mb', 'shard_downloads', 'storage_mode',
                   'download_method', 'yt5s_quality', 'yt5s_browser_count', 'fragment_concurrency',
                   'downmp3_quality', 'downmp3_timeout'}
INTEGER_FIELDS = {'worker_count': (1, 16), 'download_attempts': (1, 10), 'min_free_gb': (0, 10000),
                  'log_retention_days': (0, 3650), 'max_log_mb': (1, 1000), 'yt5s_browser_count': (1, 16),
                  'fragment_concurrency': (1, 16), 'downmp3_timeout': (30, 1800)}


def urls_from(value):
    if isinstance(value, str):
        value = [line.strip() for line in value.splitlines() if line.strip()]
    if not isinstance(value, list) or not value or len(value) > 100:
        raise ValueError('请输入 1 到 100 个视频或播放列表地址，每行一个。')
    urls = []
    for index, url in enumerate(value, 1):
        if not isinstance(url, str) or len(url) > 8192:
            raise ValueError('视频地址格式不正确。')
        url = url.strip()
        if re.fullmatch(r'[A-Za-z0-9_-]{11}', url):
            url = YOUTUBE_PREFIX + url
        parsed = urlsplit(url)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError(f'第 {index} 行请输入 11 位 YouTube 视频 ID 或有效的 HTTP / HTTPS 地址。')
        if url not in urls:
            urls.append(url)
    return urls


def validate_settings(config, *, download_only=False):
    from Utils.download_storage import storage_mode
    storage_mode(config)
    if config.get('download_method', 'yt_dlp') not in ('yt_dlp', 'yt5s', 'yt5s_fallback', 'downmp3'):
        raise ValueError('请选择有效的下载方式。')
    if config.get('yt5s_quality', '360') not in ('360', '720'):
        raise ValueError('方式二画质请选择 360p 或 720p。')
    if config.get('downmp3_quality', '720') not in ('360', '480', '720', '1080'):
        raise ValueError('方式四画质请选择 360p、480p、720p 或 1080p。')
    for key, (minimum, maximum) in INTEGER_FIELDS.items():
        value = config.get(key, DEFAULTS[key])
        if isinstance(value, str) and value.isascii() and value.isdigit():
            value = int(value)
        if type(value) is not int or not minimum <= value <= maximum:
            raise ValueError(f'{key} 必须是 {minimum} 到 {maximum} 之间的整数。')
        if key in config:
            config[key] = value
    if isinstance(config.get('shard_downloads'), str) and config['shard_downloads'] in ('true', 'false'):
        config['shard_downloads'] = config['shard_downloads'] == 'true'
    if type(config.get('shard_downloads', True)) is not bool:
        raise ValueError('按视频 ID 分目录必须为开关值。')
    if config.get('proxy_mode', 'manual') not in ('direct', 'manual', 'clash'):
        raise ValueError('请选择有效的下载连接方式。')
    for key, default in DEFAULTS.items():
        value = config.get(key, default)
        if key in INTEGER_FIELDS or key in ('shard_downloads', 'js_runtimes', 'url'):
            continue
        if not isinstance(value, str) or len(value) > 4096 or '\x00' in value:
            raise ValueError(f'{key} 必须为有效文本。')
    if not config.get('path', 'videos').strip():
        raise ValueError('素材目录不能为空。')
    if config.get('url'):
        urls_from(config['url'])
    return config


def validate_method_urls(config, urls):
    method = config.get('download_method', 'yt_dlp')
    if method in ('yt5s', 'yt5s_fallback', 'downmp3'):
        from webapp.store import source_identity
        if any(not re.fullmatch(r'[A-Za-z0-9_-]{11}', source_identity(url)[0] or '') for url in urls):
            label = '方式四' if method == 'downmp3' else '方式二、方式三'
            raise ValueError(label + '仅支持单个 YouTube 视频 ID 或视频链接；多个视频请每行一个，不支持播放列表。')


def save_settings(path, config):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                         prefix='.web-config-', suffix='.tmp', delete=False) as output:
            temporary = Path(output.name)
            clean = {key: config.get(key, default) for key, default in DEFAULTS.items()}
            yaml.safe_dump(clean, output, allow_unicode=True, sort_keys=False)
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
