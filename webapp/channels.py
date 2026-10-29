"""Download entrances and explicit, overlapping resource identities.

Different hostnames are deliberately NOT evidence of independent bandwidth.
New Bergung entrances inherit the existing groups until the operator changes them.
"""
from dataclasses import dataclass
import re
from urllib.parse import urlsplit


SITE = 'https://bergung.hoffnungfuerdiezukunft.net/'
API = 'https://bergung-api.hoffnungfuerdiezukunft.net/'
IDENTIFIER = re.compile(r'[A-Za-z0-9_-]{1,64}')


@dataclass(frozen=True)
class Channel:
    id: str
    adapter: str
    label: str
    upstream_group: str
    resolver_group: str
    quota_group: str
    transfer_limit: int = 8
    resolver_limit: int = 4
    connection_limit: int = 16
    weight: int = 1
    site_url: str = ''
    api_url: str = ''
    task_limit: int = 8
    verification: str = 'required'


def _endpoint(value):
    if not isinstance(value, str) or len(value) > 2048:
        raise ValueError('渠道地址必须是有效的 HTTPS 地址。')
    parsed = urlsplit(value)
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment):
        raise ValueError('渠道地址请使用 HTTPS，不带账号、查询参数或片段。')
    return value.rstrip('/') + '/'


def validate_channels(config):
    if config.get('scheduler_mode', 'ordered_fallback') not in ('ordered_fallback', 'balanced', 'auto'):
        raise ValueError('下载策略请选择按顺序回退、同类渠道分流或自动择优。')
    entries = config.get('download_channels', [])
    if not isinstance(entries, list) or len(entries) > 16:
        raise ValueError('最多配置 16 个 Bergung 兼容渠道。')
    allowed = set(Channel.__dataclass_fields__)
    normalized, ids = [], set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError('渠道配置包含未知字段。')
        values = dict(entry)
        # Early Cobalt configurations explicitly selected the browser resolver.
        # All supported entrances use that resolver; do not invent an API mode.
        if values.pop('resolver_mode', 'browser') != 'browser':
            raise ValueError('兼容渠道目前只支持 browser 解析方式。')
        if set(values) - allowed:
            raise ValueError('渠道配置包含未知字段。')
        identity = values.get('id', '')
        if not isinstance(identity, str) or not IDENTIFIER.fullmatch(identity) or identity in ids:
            raise ValueError('渠道标识必须唯一，使用 1～64 位字母、数字、下划线或短横线。')
        # Additional adapters can be registered here when their protocol is implemented.
        if values.get('adapter', 'bergung') != 'bergung' or identity in ('yt5s', 'yt1d', 'yt_dlp', 'downmp3'):
            raise ValueError('自定义渠道目前支持 Bergung 兼容网站。')
        values['adapter'] = 'bergung'
        if values.setdefault('verification', 'required') not in ('required', 'auto', 'none'):
            raise ValueError('网站验证方式请选择 required、auto 或 none。')
        for name in ('upstream_group', 'resolver_group', 'quota_group'):
            value = values.setdefault(name, 'unverified-media' if name == 'upstream_group' else 'bergung')
            if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
                raise ValueError('共享资源组必须使用 1～64 位字母、数字、下划线或短横线。')
        label = values.setdefault('label', identity)
        if not isinstance(label, str) or not label.strip() or len(label) > 80 or any(ord(c) < 32 for c in label):
            raise ValueError('渠道名称必须为 1～80 位有效文本。')
        for name, default, maximum in (('transfer_limit', 8, 64), ('resolver_limit', 4, 16),
                                        ('connection_limit', 16, 128), ('weight', 1, 10), ('task_limit', 8, 64)):
            value = values.setdefault(name, default)
            if type(value) is not int or not 1 <= value <= maximum:
                raise ValueError(f'渠道 {name} 必须是 1 到 {maximum} 之间的整数。')
        values['site_url'] = _endpoint(values.get('site_url', SITE))
        values['api_url'] = _endpoint(values.get('api_url', API))
        normalized.append(values)
        ids.add(identity)
    if 'download_channels' in config:
        config['download_channels'] = normalized
    return normalized


def registry(config):
    from webapp.settings import PROVIDERS, download_methods
    extra = [Channel(**item) for item in validate_channels(dict(config))]
    channels = []
    for adapter in download_methods(config):
        upstream = 'youtube-origin' if adapter == 'yt_dlp' else 'unverified-media'
        default = Channel(adapter, adapter, PROVIDERS[adapter], upstream, adapter, adapter,
                                transfer_limit=32 if config.get('scheduler_mode') == 'auto' else 8,
                                connection_limit=64 if config.get('scheduler_mode') == 'auto' else 16,
                                task_limit=32 if config.get('scheduler_mode') == 'auto' else 8,
                                site_url=SITE if adapter == 'bergung' else '',
                                api_url=API if adapter == 'bergung' else '')
        channels.append(next((channel for channel in extra if channel.id == adapter), default))
        for channel in extra:
            if channel.adapter != adapter:
                continue
            if channel.id != adapter:
                channels.append(channel)
    return channels


def group_caps(channels):
    """Conflicting alias settings always choose the smaller shared allowance."""
    caps = {}
    for channel in channels:
        for kind, group, limit in (('upstream', channel.upstream_group, channel.transfer_limit),
                                    ('resolver', channel.resolver_group, channel.resolver_limit),
                                    ('quota', channel.quota_group, channel.transfer_limit),
                                    ('connections', channel.upstream_group, channel.connection_limit)):
            key = kind + ':' + group
            caps[key] = min(caps.get(key, limit), limit)
    return caps


def execution_config(config, channel):
    return {**config, 'download_method': channel.adapter, '_channel_id': channel.id,
            '_bergung_site_url': channel.site_url, '_bergung_api_url': channel.api_url,
            '_cobalt_verification': channel.verification,
            '_resolver_deadline': 90 if config.get('scheduler_mode') == 'auto' else 0}


def backend_key(channel):
    """Identical APIs share admission even if their frontends/groups differ."""
    import hashlib
    parsed = urlsplit(channel.api_url)
    endpoint = ((parsed.hostname or '').lower().rstrip('.') + ':' + str(parsed.port or 443)
                + (parsed.path.rstrip('/') or '/')) if channel.api_url else channel.adapter
    return hashlib.sha256(endpoint.encode()).hexdigest()[:24]


def observed_group(channel, info):
    # A redirect to Googlevideo is the same source bandwidth across entrances.
    # Tunnel URLs and other CDNs retain the operator's conservative mapping.
    host = (urlsplit(info.get('url', '')).hostname or '').lower()
    if host == 'googlevideo.com' or host.endswith('.googlevideo.com'):
        return 'youtube-origin'
    return channel.upstream_group
