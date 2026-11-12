"""Configuration metadata shared by validation, persistence and fleet projections.

Keep this module dependency-free apart from the standard library: storage and
download helpers import it too. Classifications describe existing consumers;
they do not imply that a value can be hot-applied to an already running task.
Domain and cross-field validation remains with the relevant service.
"""
from dataclasses import dataclass
from types import MappingProxyType


@dataclass(frozen=True)
class Setting:
    # Defaults are immutable scalars; mutable collections use a fresh factory.
    default: str | int | bool | None = ''
    factory: type[list] | None = None
    limits: tuple[int, int] | None = None
    scope: str = 'local'
    download: bool = True
    resource_override: bool = False
    persist_runtime: bool = False
    pipeline_key: bool = False
    family: str = ''
    custom: bool = False
    secret: bool = False

    def __post_init__(self):
        if type(self.default) not in (str, int, bool, type(None)):
            raise ValueError('Use a factory for mutable configuration defaults.')
        if self.scope not in ('local', 'policy'):
            raise ValueError('A setting must have exactly one fleet scope.')
        if self.limits is not None:
            low, high = self.limits
            if type(self.default) is not int or not low <= self.default <= high:
                raise ValueError('Integer defaults must be within their validation range.')
        if self.resource_override and self.limits is None:
            raise ValueError('Resource overrides need an integer validation range.')

    @property
    def value_type(self):
        return self.factory or type(self.default)

    def default_value(self):
        return self.factory() if self.factory is not None else self.default


# A setting's default, range and consumer memberships are declared together.
# Preserve order: it is also the order used when saving config.yaml.
SETTINGS = MappingProxyType({
    'path': Setting('videos'),
    'dir': Setting(),
    'url': Setting(custom=True),
    'proxy': Setting(),
    'proxy_mode': Setting('manual'),
    'youtube_cookie': Setting(persist_runtime=True),
    'browser_executable': Setting(download=False),
    'download_format': Setting(scope='policy', download=False),
    'js_runtimes': Setting(None, download=False, custom=True),
    'worker_count': Setting(1, limits=(1, 64), resource_override=True, persist_runtime=True),
    'download_attempts': Setting(3, limits=(1, 10), scope='policy'),
    'min_free_gb': Setting(2, limits=(0, 10000), resource_override=True, persist_runtime=True),
    'log_retention_days': Setting(0, limits=(0, 3650), persist_runtime=True),
    'max_log_mb': Setting(2, limits=(1, 1000), persist_runtime=True),
    'shard_downloads': Setting(True),
    'storage_mode': Setting('local', scope='policy', persist_runtime=True),
    'download_method': Setting('yt_dlp', scope='policy', persist_runtime=True, pipeline_key=True),
    'yt5s_quality': Setting('best', scope='policy'),
    'yt5s_browser_count': Setting(2, limits=(1, 16), resource_override=True, persist_runtime=True),
    'download_methods': Setting(None, scope='policy', persist_runtime=True, pipeline_key=True, custom=True),
    'fragment_concurrency': Setting(4, limits=(1, 16), scope='policy', persist_runtime=True, pipeline_key=True),
    'throughput_mode': Setting('manual', persist_runtime=True),
    'bandwidth_mbps': Setting(0, limits=(0, 100000), persist_runtime=True),
    'db_budget_mode': Setting('auto', persist_runtime=True, pipeline_key=True),
    'db_connection_budget': Setting(64, limits=(8, 10000), persist_runtime=True, pipeline_key=True),
    'scheduler_mode': Setting('ordered_fallback', scope='policy', persist_runtime=True, pipeline_key=True),
    'download_channels': Setting(factory=list, scope='policy', persist_runtime=True, pipeline_key=True, custom=True),
    'auto_channel_attempts': Setting(3, limits=(1, 6), scope='policy'),
    'download_connection_limit': Setting(32, limits=(2, 128), persist_runtime=True, pipeline_key=True),
    'shared_parser': Setting(True),
    'subtitle_mode': Setting('optional', scope='policy'),
    'subtitle_languages': Setting('en,zh-Hans,zh-Hant', scope='policy'),
    'builtin_transfer_limit': Setting(12, limits=(0, 64), scope='policy', resource_override=True,
                                      persist_runtime=True, pipeline_key=True),
    'builtin_resolver_limit': Setting(4, limits=(0, 16), scope='policy', persist_runtime=True, pipeline_key=True),
    'youtube_source_address': Setting(),
    'youtube_start_interval': Setting(120, limits=(0, 600)),
    'youtube_task_rest_interval': Setting(0, limits=(0, 3600), persist_runtime=True, pipeline_key=True),
    'youtube_cookie_fallback': Setting(False),
    'youtube_cookie_usage': Setting('auto'),
    'youtube_throttle_cooldown': Setting(300, limits=(30, 3600)),
    'youtube_sleep_preset': Setting(True),
    'youtube_cookie_pool': Setting(factory=list, persist_runtime=True, pipeline_key=True, custom=True),
    'youtube_cookie_session_limit': Setting(1, limits=(1, 64), resource_override=True,
                                          persist_runtime=True, pipeline_key=True),
    'youtube_cookie_pool_start_interval': Setting(5, limits=(0, 600), resource_override=True,
                                                persist_runtime=True, pipeline_key=True),
    'youtube_cookie_pool_worker_limit': Setting(20, limits=(0, 64), resource_override=True,
                                              persist_runtime=True, pipeline_key=True),
    'youtube_cookie_pool_preparation_workers': Setting(4, limits=(0, 16), resource_override=True,
                                                     persist_runtime=True, pipeline_key=True),
    'youtube_pacing_mode': Setting('manual', family='pacing'),
    'youtube_hourly_start_limit': Setting(30, limits=(0, 5000), family='pacing'),
    'youtube_daily_start_limit': Setting(720, limits=(0, 5000), family='pacing'),
    'postprocess_limit': Setting(2, limits=(1, 8)),
    'downmp3_quality': Setting('best', scope='policy'),
    'downmp3_timeout': Setting(300, limits=(30, 1800), scope='policy'),
    'yt1d_quality': Setting('best', scope='policy'),
    'yt1d_timeout': Setting(180, limits=(30, 1800), scope='policy'),
    'bergung_quality': Setting('best', scope='policy'),
    'bergung_timeout': Setting(180, limits=(30, 1800), scope='policy'),
    'disk_budget_gb': Setting(20, limits=(1, 100000), family='disk', persist_runtime=True),
    'disk_test_budget_gb': Setting(16, limits=(1, 100000), family='disk', persist_runtime=True),
    'disk_browser_budget_mb': Setting(1024, limits=(1, 100000), family='disk', persist_runtime=True),
    'disk_log_budget_mb': Setting(256, limits=(1, 100000), family='disk', persist_runtime=True),
    'disk_import_budget_mb': Setting(1024, limits=(1, 100000), family='disk', persist_runtime=True),
    'disk_cache_budget_mb': Setting(256, limits=(1, 100000), family='disk', persist_runtime=True),
    'disk_min_free_gb': Setting(10, limits=(0, 100000), family='disk', persist_runtime=True),
    'disk_resume_free_gb': Setting(15, limits=(0, 100000), family='disk', persist_runtime=True),
    'disk_upload_limit_mb': Setting(256, limits=(1, 100000), family='disk', persist_runtime=True),
    'disk_success_log_hours': Setting(6, limits=(1, 100000), family='disk', persist_runtime=True),
    'disk_failure_log_days': Setting(7, limits=(1, 100000), family='disk', persist_runtime=True),
    'oss_endpoint': Setting(family='oss'),
    'oss_bucket': Setting(family='oss'),
    'oss_prefix': Setting('videos', family='oss'),
    'oss_access_key_id': Setting(family='oss'),
    'oss_access_key_secret': Setting(family='oss', secret=True),
    'oss_security_token': Setting(family='oss', secret=True),
    'oss_upload_threads': Setting(2, limits=(1, 8), family='oss'),
    'oss_part_size_mb': Setting(8, limits=(4, 64), family='oss'),
    'oss_upload_limit': Setting(2, limits=(1, 8), family='oss'),
})


def default_settings(*, family=None):
    """Return fresh defaults without sharing a mutable pool/channel selection."""
    return {key: spec.default_value() for key, spec in SETTINGS.items()
            if family is None or spec.family == family}


# Compatibility projections; existing import paths re-export these names.
DEFAULTS = default_settings()
DOWNLOAD_FIELDS = {key for key, spec in SETTINGS.items() if spec.download}
INTEGER_FIELDS = {key: spec.limits for key, spec in SETTINGS.items() if spec.limits is not None}
TEXT_FIELDS = {key for key, spec in SETTINGS.items() if spec.value_type is str and not spec.custom}
POLICY_FIELDS = {key for key, spec in SETTINGS.items() if spec.scope == 'policy'}
LOCAL_FIELDS = {key for key, spec in SETTINGS.items() if spec.scope == 'local'}
RESOURCE_LIMITS = {key: spec.limits for key, spec in SETTINGS.items() if spec.resource_override}
RUNTIME_FIELDS = tuple(key for key, spec in SETTINGS.items() if spec.persist_runtime)
PIPELINE_KEY_FIELDS = tuple(key for key, spec in SETTINGS.items() if spec.pipeline_key)
DISK_DEFAULTS = default_settings(family='disk')
DISK_FIELDS = set(DISK_DEFAULTS)
OSS_DEFAULTS = default_settings(family='oss')
OSS_FIELDS = set(OSS_DEFAULTS)
OSS_INTEGER_FIELDS = {key: INTEGER_FIELDS[key] for key in OSS_DEFAULTS if key in INTEGER_FIELDS}
OSS_SECRETS = {key for key, spec in SETTINGS.items() if spec.secret}
PACING_FIELDS = {key for key, spec in SETTINGS.items() if spec.family == 'pacing'}

RETIRED_FIELDS = frozenset({'builtin_connection_limit'})


def discard_retired_settings(config):
    """Ignore keys still sent by cached frontends or old task snapshots, in place."""
    for key in RETIRED_FIELDS:
        config.pop(key, None)
    return config
