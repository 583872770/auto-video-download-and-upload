"""Validate settings used by the YouTube download service."""
import re
from pathlib import Path
import tempfile
from urllib.parse import urlsplit

YOUTUBE_PREFIX = 'https://www.youtube.com/watch?v='
PROVIDERS = {'yt_dlp': 'yt-dlp', 'yt5s': 'yt5s', 'downmp3': 'downmp3', 'yt1d': 'yt1d',
             'bergung': 'Bergung'}

import yaml
from webapp.disk_budget import DISK_DEFAULTS, DISK_FIELDS

DEFAULTS = {
    'path': 'videos', 'dir': '', 'url': '', 'proxy': '', 'proxy_mode': 'manual', 'youtube_cookie': '',
    'browser_executable': '', 'download_format': '', 'js_runtimes': None,
    'worker_count': 1, 'download_attempts': 3, 'min_free_gb': 2,
    'log_retention_days': 0, 'max_log_mb': 2, 'shard_downloads': True, 'storage_mode': 'local',
    'download_method': 'yt_dlp', 'yt5s_quality': 'best', 'yt5s_browser_count': 2,
    'download_methods': None,
    'fragment_concurrency': 4,
    'throughput_mode': 'manual', 'bandwidth_mbps': 0,
    'scheduler_mode': 'ordered_fallback', 'download_channels': [],
    'auto_channel_attempts': 3,
    'download_connection_limit': 32, 'shared_parser': True,
    'subtitle_mode': 'optional', 'subtitle_languages': 'en,zh-Hans,zh-Hant',
    'builtin_transfer_limit': 0, 'builtin_resolver_limit': 4, 'builtin_connection_limit': 0,
    'downmp3_quality': 'best', 'downmp3_timeout': 300,
    'yt1d_quality': 'best', 'yt1d_timeout': 180,
    'bergung_quality': 'best', 'bergung_timeout': 180,
    **DISK_DEFAULTS,
}
DOWNLOAD_FIELDS = {'path', 'dir', 'url', 'proxy', 'proxy_mode', 'youtube_cookie', 'worker_count',
                   'download_attempts', 'min_free_gb', 'log_retention_days', 'max_log_mb', 'shard_downloads', 'storage_mode',
                   'download_method', 'download_methods', 'yt5s_quality', 'yt5s_browser_count', 'fragment_concurrency',
                   'downmp3_quality', 'downmp3_timeout', 'yt1d_quality', 'yt1d_timeout',
                   'bergung_quality', 'bergung_timeout'}
DOWNLOAD_FIELDS |= DISK_FIELDS
DOWNLOAD_FIELDS |= {'throughput_mode', 'bandwidth_mbps'}
DOWNLOAD_FIELDS |= {'scheduler_mode', 'download_channels', 'download_connection_limit', 'shared_parser', 'auto_channel_attempts'}
DOWNLOAD_FIELDS |= {'subtitle_mode', 'subtitle_languages', 'builtin_transfer_limit', 'builtin_resolver_limit', 'builtin_connection_limit'}
INTEGER_FIELDS = {'worker_count': (1, 64), 'download_attempts': (1, 10), 'min_free_gb': (0, 10000),
                  'log_retention_days': (0, 3650), 'max_log_mb': (1, 1000), 'yt5s_browser_count': (1, 16),
                  'fragment_concurrency': (1, 16), 'downmp3_timeout': (30, 1800), 'yt1d_timeout': (30, 1800),
                  'bergung_timeout': (30, 1800)}
INTEGER_FIELDS.update({key: (0 if key in ('disk_min_free_gb', 'disk_resume_free_gb') else 1, 100000)
                       for key in DISK_FIELDS})
INTEGER_FIELDS['bandwidth_mbps'] = (0, 100000)
INTEGER_FIELDS['download_connection_limit'] = (2, 128)
INTEGER_FIELDS['auto_channel_attempts'] = (1, 6)
INTEGER_FIELDS.update(builtin_transfer_limit=(0, 64), builtin_resolver_limit=(1, 16), builtin_connection_limit=(0, 128))


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


def download_methods(config):
    """Read an ordered provider list, including configurations saved before this UI."""
    methods = config.get('download_methods')
    if methods is None:
        method = config.get('download_method', 'yt_dlp')
        methods = ['yt5s', 'yt_dlp'] if method == 'yt5s_fallback' else [method]
    if (not isinstance(methods, list) or not 1 <= len(methods) <= len(PROVIDERS)
            or any(not isinstance(method, str) or method not in PROVIDERS for method in methods)
            or len(set(methods)) != len(methods)):
        raise ValueError('请至少选择一种下载器，并按优先级排列；下载器不能重复。')
    return list(methods)


def validate_settings(config, *, download_only=False):
    if config.get('subtitle_mode', 'optional') not in ('optional', 'required', 'off'):
        raise ValueError('字幕请选择后台补下载、必须完整或关闭。')
    languages = config.get('subtitle_languages', 'en,zh-Hans,zh-Hant')
    if not isinstance(languages, str) or not 1 <= len(languages.split(',')) <= 8 or any(
            not re.fullmatch(r'[A-Za-z0-9-]{1,32}', language.strip()) for language in languages.split(',')):
        raise ValueError('字幕语言请填写最多 8 个语言代码，用逗号分隔，例如 en,zh-Hans。')
    from Utils.download_storage import storage_mode
    storage_mode(config)
    if config.get('throughput_mode', 'manual') not in ('manual', 'auto'):
        raise ValueError('吞吐调节请选择手动或自动。')
    if config.get('download_method', 'yt_dlp') not in (*PROVIDERS, 'yt5s_fallback'):
        raise ValueError('请选择有效的下载方式。')
    download_methods(config)
    from webapp.channels import validate_channels
    validate_channels(config)
    if config.get('scheduler_mode') == 'auto':
        config['throughput_mode'] = 'auto'
    if type(config.get('shared_parser', True)) is not bool:
        if config.get('shared_parser') in ('true', 'false'):
            config['shared_parser'] = config['shared_parser'] == 'true'
        else:
            raise ValueError('复用解析浏览器必须为开关值。')
    if config.get('yt5s_quality', 'best') not in ('best', '360', '720'):
        raise ValueError('yt5s 画质请选择尽可能最高画质、360p 或 720p。')
    if config.get('downmp3_quality', 'best') not in ('best', '360', '480', '720', '1080'):
        raise ValueError('downmp3 画质请选择尽可能最高画质、360p、480p、720p 或 1080p。')
    if config.get('yt1d_quality', 'best') not in ('best', '360', '480', '720', '1080', '1440', '2160'):
        raise ValueError('yt1d 画质请选择尽可能最高画质或 360p、480p、720p、1080p、1440p、2160p。')
    if config.get('bergung_quality', 'best') not in ('best', '360', '480', '720', '1080', '1440', '2160', '4320'):
        raise ValueError('Bergung 画质请选择尽可能最高画质或 360p、480p、720p、1080p、1440p、2160p、4320p。')
    for key, (minimum, maximum) in INTEGER_FIELDS.items():
        value = config.get(key, DEFAULTS[key])
        if isinstance(value, str) and value.isascii() and value.isdigit():
            value = int(value)
        if type(value) is not int or not minimum <= value <= maximum:
            raise ValueError(f'{key} 必须是 {minimum} 到 {maximum} 之间的整数。')
        if key in config:
            config[key] = value
    if config.get('disk_resume_free_gb', 15) < config.get('disk_min_free_gb', 10):
        raise ValueError('磁盘恢复水位不能低于暂停水位。')
    if config.get('disk_upload_limit_mb', 256) > config.get('disk_import_budget_mb', 1024):
        raise ValueError('单次上传上限不能大于导入临时文件预算。')
    if isinstance(config.get('shard_downloads'), str) and config['shard_downloads'] in ('true', 'false'):
        config['shard_downloads'] = config['shard_downloads'] == 'true'
    if type(config.get('shard_downloads', True)) is not bool:
        raise ValueError('按视频 ID 分目录必须为开关值。')
    if config.get('proxy_mode', 'manual') not in ('direct', 'manual', 'clash'):
        raise ValueError('请选择有效的下载连接方式。')
    for key, default in DEFAULTS.items():
        value = config.get(key, default)
        if key in INTEGER_FIELDS or key in ('shard_downloads', 'js_runtimes', 'url', 'download_methods', 'download_channels', 'shared_parser'):
            continue
        if not isinstance(value, str) or len(value) > 4096 or '\x00' in value:
            raise ValueError(f'{key} 必须为有效文本。')
    if not config.get('path', 'videos').strip():
        raise ValueError('素材目录不能为空。')
    if config.get('url'):
        urls_from(config['url'])
    return config


def validate_method_urls(config, urls):
    if any(method != 'yt_dlp' for method in download_methods(config)):
        from webapp.store import source_identity
        if any(not re.fullmatch(r'[A-Za-z0-9_-]{11}', source_identity(url)[0] or '') for url in urls):
            raise ValueError('yt5s、downmp3、yt1d、Bergung 仅支持单个 YouTube 视频 ID 或视频链接；多个视频请每行一个，不支持播放列表。')


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
