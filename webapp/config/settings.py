"""Validate settings used by the YouTube download service."""
import re
import ipaddress
from pathlib import Path
import tempfile
from urllib.parse import urlsplit

YOUTUBE_PREFIX = 'https://www.youtube.com/watch?v='
import yaml
from Utils.oss_storage import validate_oss
from webapp.config.schema import DEFAULTS, INTEGER_FIELDS, TEXT_FIELDS


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


def validate_settings(config):
    unknown = [key for key in config if key not in DEFAULTS and not key.startswith('_')]
    if unknown:
        raise ValueError('不支持的配置项：' + '、'.join(sorted(unknown)))
    pool = config.get('youtube_cookie_pool', [])
    if (not isinstance(pool, list) or len(pool) > 50 or
            any(not isinstance(item, str) or not re.fullmatch(r'[a-f0-9]{32}', item) for item in pool) or
            len(set(pool)) != len(pool)):
        raise ValueError('Cookie 账号池必须为不重复的已保存 Cookie ID 列表，最多 50 个。')
    source = config.get('youtube_source_address', '')
    try:
        if source:
            ipaddress.ip_address(source)
    except (ValueError, TypeError):
        raise ValueError('直连源地址必须是本机可用的 IPv4 或 IPv6 地址；留空自动选择。') from None
    sleep_preset = config.get('youtube_sleep_preset', True)
    if sleep_preset in ('true', 'false'):
        config['youtube_sleep_preset'] = sleep_preset == 'true'
    elif type(sleep_preset) is not bool:
        raise ValueError('yt-dlp 请求等待预设必须为开关值。')
    if type(config.get('download_auto_reduce', True)) is not bool:
        raise ValueError('自动降低下载进程上限必须为开关值。')
    from Utils.download_storage import storage_mode
    storage_mode(config)
    if config.get('storage_mode') == 'oss':
        validate_oss(config)
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
        if key not in TEXT_FIELDS:
            continue
        if not isinstance(value, str) or len(value) > 4096 or '\x00' in value:
            raise ValueError(f'{key} 必须为有效文本。')
    if not config.get('path', 'videos').strip():
        raise ValueError('素材目录不能为空。')
    return config


def save_settings(path, config):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                         prefix='.web-config-', suffix='.tmp', delete=False) as output:
            temporary = Path(output.name)
            from webapp.config.schema import SETTINGS
            clean = {key: config.get(key, default) for key, default in DEFAULTS.items() if SETTINGS[key].scope != 'task'}
            yaml.safe_dump(clean, output, allow_unicode=True, sort_keys=False)
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
