"""Validate web-editable settings while preserving unrelated CLI options."""
import math
import re
from pathlib import Path
import tempfile
from urllib.parse import urlsplit

YOUTUBE_PREFIX = 'https://www.youtube.com/watch?v='

import yaml

DEFAULTS = {
    'path': 'videos', 'dir': '', 'url': '', 'proxy': '', 'proxy_mode': 'manual', 'youtube_cookie': '',
    'platforms': ['douyin', 'kuaishou'], 'segment_seconds': 180,
    'time_list': [8, 11, 14, 16, 19, 21], 'upload_timeout_seconds': 1800,
    'browser_executable': '', 'douyin_cookie': 'Cookies/douyin.json',
    'kuaishou_cookie': 'Cookies/kuaishou.json',
    'douyin_visibility': 'public', 'kuaishou_visibility': 'public',
    'douyin_tags': [], 'kuaishou_tags': [],
    'worker_count': 1, 'download_attempts': 3, 'min_free_gb': 2,
    'log_retention_days': 0, 'max_log_mb': 10, 'shard_downloads': True, 'storage_mode': 'local',
    'download_method': 'yt_dlp', 'yt5s_quality': '360',
}
DOWNLOAD_FIELDS = {'path', 'dir', 'url', 'proxy', 'proxy_mode', 'youtube_cookie', 'worker_count',
                   'download_attempts', 'min_free_gb', 'log_retention_days', 'max_log_mb', 'shard_downloads', 'storage_mode',
                   'download_method', 'yt5s_quality'}
INTEGER_FIELDS = {'worker_count': (1, 16), 'download_attempts': (1, 10), 'min_free_gb': (0, 10000),
                  'log_retention_days': (0, 3650), 'max_log_mb': (1, 1000)}


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
    if config.get('download_method', 'yt_dlp') not in ('yt_dlp', 'yt5s', 'yt5s_fallback'):
        raise ValueError('请选择有效的下载方式。')
    if config.get('yt5s_quality', '360') not in ('360', '720'):
        raise ValueError('方式二画质请选择 360p 或 720p。')
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
        if download_only and key not in DOWNLOAD_FIELDS:
            continue
        value = config.get(key, default)
        if key in INTEGER_FIELDS or key == 'shard_downloads' or key in ('platforms', 'time_list', 'segment_seconds', 'upload_timeout_seconds', 'url') or key.endswith('_tags'):
            continue
        if not isinstance(value, str) or len(value) > 4096 or '\x00' in value:
            raise ValueError(f'{key} 必须为有效文本。')
    if not config.get('path', 'videos').strip():
        raise ValueError('素材目录不能为空。')
    if config.get('url'):
        urls_from(config['url'])
    if download_only:
        return config
    for key in ('segment_seconds', 'upload_timeout_seconds'):
        value = config.get(key, DEFAULTS[key])
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            raise ValueError(f'{key} 必须是大于零的数字。')
    hours = config.get('time_list', DEFAULTS['time_list'])
    if isinstance(hours, str):
        try:
            hours = [int(item.strip()) for item in hours.replace('，', ',').split(',')]
        except ValueError as error:
            raise ValueError('发布时间应为逗号分隔的整数小时。') from error
    if not isinstance(hours, list) or not hours or any(type(h) is not int or not 0 <= h <= 23 for h in hours):
        raise ValueError('发布时间必须是 0 到 23 的整数小时。')
    config['time_list'] = sorted(set(hours))
    platforms = config.get('platforms', DEFAULTS['platforms'])
    if not isinstance(platforms, list) or not platforms or any(p not in ('douyin', 'kuaishou', 'xigua') for p in platforms):
        raise ValueError('请选择有效的发布平台。')
    config['platforms'] = list(dict.fromkeys(platforms))
    for platform in ('douyin', 'kuaishou'):
        if config.get(f'{platform}_visibility', 'private') not in ('private', 'public', 'friends'):
            raise ValueError('可见范围必须是 private / public / friends。')
        tags = config.get(f'{platform}_tags', [])
        if not isinstance(tags, list) or any(not isinstance(tag, str) or not tag.strip() or len(tag) > 100 for tag in tags):
            raise ValueError('话题必须为非空文本组成的列表，每个不超过 100 字。')
    return config


def validate_method_urls(config, urls):
    if config.get('download_method', 'yt_dlp') in ('yt5s', 'yt5s_fallback'):
        from webapp.store import source_identity
        if any(not re.fullmatch(r'[A-Za-z0-9_-]{11}', source_identity(url)[0] or '') for url in urls):
            raise ValueError('方式二、方式三仅支持单个 YouTube 视频 ID 或视频链接；多个视频请每行一个，不支持播放列表。')


def save_settings(path, config):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                         prefix='.web-config-', suffix='.tmp', delete=False) as output:
            temporary = Path(output.name)
            yaml.safe_dump(config, output, allow_unicode=True, sort_keys=False)
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
