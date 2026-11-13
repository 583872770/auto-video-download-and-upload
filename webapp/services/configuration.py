"""ConfigurationService operations independent of route registration."""
from flask import g, has_request_context
from Utils.config import resolve_path, video_directory
from webapp.config.settings import DEFAULTS, DOWNLOAD_FIELDS, download_methods, validate_settings
from webapp.config.schema import discard_retired_settings


class ConfigurationService:
    def __init__(self, context, cookies):
        self.context = context
        self.cookies = cookies

    def read_config(self):
        if self.context.fleet and self.context.fleet.can_manage and not (has_request_context() and g.fleet_rpc):
            selected_id, rpc = self.context.dependencies.selected_id, self.context.dependencies.rpc
            if selected_id() != self.context.fleet.id:
                # Paths and the complete configuration belong to the selected
                # node. Resolving a peer's relative paths against this entry
                # node's base silently describes a different directory.
                return dict(rpc(self.context.fleet, selected_id(), '/api/config'))
        config, base = self.context.settings()
        editable = {key: config.get(key, default) for key, default in DEFAULTS.items()}
        editable['download_methods'] = download_methods(config)
        from Utils.oss_storage import public_oss_config, OSS_SECRETS
        editable = public_oss_config(editable)
        for key in OSS_SECRETS:
            editable[key + '_configured'] = bool(config.get(key) or config.get(key + '_configured'))
        from webapp.db_pool_calculator import connection_budget
        return dict(config=editable, database_budget=connection_budget(config),
                       config_path=str(self.context.config_path), media_directory=str(video_directory(config, base)),
                       instance_id=self.context.fleet.id if self.context.fleet else None)

    def update_config(self, data):
        # A cached older frontend may still submit this retired field.
        discard_retired_settings(data)
        if self.context.fleet and self.context.fleet.can_manage and not (has_request_context() and g.fleet_rpc):
            from webapp.services.instances import update_configuration
            selected_id = self.context.dependencies.selected_id
            if selected_id() != self.context.fleet.id:
                return update_configuration(self.context.fleet, data, target=selected_id(),
                                            rpc=self.context.dependencies.rpc)
        if any(key not in DEFAULTS for key in data):
            raise ValueError('包含不支持在页面修改的配置项。')
        with self.context.media_lock, self.context.settings_lock:
            config, _ = self.context.settings()
            if any(key in data and data[key] != config.get(key, DEFAULTS[key])
                   for key in ('path', 'dir', 'storage_mode')) and self.context.store.media_clear_busy():
                return dict(error='素材库正在清空，完成后再修改保存目录或保存方式。'), 409
            if any(key in data for key in ('proxy', 'proxy_mode', 'youtube_cookie', 'youtube_source_address')):
                self.cookies.require_login_idle()
            if any(key in data and data[key] != config.get(key, DEFAULTS[key]) for key in ('proxy', 'proxy_mode', 'youtube_source_address')):
                if self.context.store.download_connection_busy():
                    return dict(error='请先停止全部下载并等待当前任务结束，再修改代理连接方式。'), 409
            from Utils.oss_storage import OSS_SECRETS
            # Blank secret fields mean keep the saved credential. Never send
            # credentials back to the browser to populate password controls.
            config.update({k: v for k, v in data.items() if k not in OSS_SECRETS or v})
            if 'download_methods' in data:
                config['download_method'] = download_methods(config)[0]
            elif 'download_method' in data:
                config['download_methods'] = None  # Compatibility with old clients/configuration.
            validate_settings(config, download_only=set(data) <= DOWNLOAD_FIELDS)
            from webapp.db_pool_calculator import connection_budget
            connection_budget(config)  # Reject impossible reservations before any persistence.
            if 'youtube_cookie_pool' in data or 'youtube_cookie' in data:
                from webapp.cookie_profiles import CookieProfiles
                profiles = CookieProfiles(self.context.config_path.parent, self.context.store)
                for identity in config.get('youtube_cookie_pool', []):
                    profiles.require(identity)
                if 'youtube_cookie' in data and config.get('youtube_cookie'):
                    profiles.restore_selected(config['youtube_cookie'])
            if self.context.fleet and self.context.fleet.can_download:
                from webapp.fleet import RESOURCE_LIMITS
                limits = {key: config[key] for key in RESOURCE_LIMITS if key in data}
                if limits:
                    self.context.fleet.control(self.context.fleet.id, limits)
            self.context.dependencies.save_settings(self.context.config_path, config)
            self.context.sync_runtime(config, self.context.config_path.parent)
            if any(key in data for key in ('proxy', 'proxy_mode', 'youtube_cookie', 'youtube_source_address')):
                self.context.youtube_auth.invalidate()
        return dict(message='配置已保存；下载器、画质等策略从后续领取的视频生效，无需暂停或重启。'
                       '配置仅作用于当前实例，正在执行的视频保持本次配置。')
