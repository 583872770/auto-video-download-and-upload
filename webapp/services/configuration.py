"""ConfigurationService operations independent of route registration."""
from flask import g, has_request_context
from webapp.config.settings import DEFAULTS, DOWNLOAD_FIELDS, validate_settings


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
        config, _ = self.context.settings()
        from webapp.task_storage import FIELDS
        editable = {key: config.get(key, default) for key, default in DEFAULTS.items() if key not in FIELDS}
        from webapp.db_pool_calculator import connection_budget
        return dict(config=editable, database_budget=connection_budget(config),
                       config_path=str(self.context.config_path), media_directory='由任务中心的文件保存配置决定',
                       instance_id=self.context.fleet.id if self.context.fleet else None)

    def update_config(self, data):
        if self.context.fleet and self.context.fleet.can_manage and not (has_request_context() and g.fleet_rpc):
            from webapp.services.instances import update_configuration
            selected_id = self.context.dependencies.selected_id
            if selected_id() != self.context.fleet.id:
                return update_configuration(self.context.fleet, data, target=selected_id(),
                                            rpc=self.context.dependencies.rpc)
        from webapp.task_storage import FIELDS
        if set(data) & FIELDS:
            raise ValueError('文件保存配置归属客户任务，请在任务中心修改。')
        if any(key not in DEFAULTS for key in data):
            raise ValueError('包含不支持在页面修改的配置项。')
        with self.context.media_lock, self.context.settings_lock:
            config, _ = self.context.settings()
            config = {key: value for key, value in config.items() if key not in FIELDS}
            if any(key in data for key in ('proxy', 'proxy_mode', 'youtube_source_address')):
                self.cookies.require_login_idle()
            if any(key in data and data[key] != config.get(key, DEFAULTS[key]) for key in ('proxy', 'proxy_mode', 'youtube_source_address')):
                if self.context.store.download_connection_busy():
                    return dict(error='请先停止全部下载并等待当前任务结束，再修改代理连接方式。'), 409
            config.update(data)
            validate_settings(config, download_only=set(data) <= DOWNLOAD_FIELDS)
            from webapp.db_pool_calculator import connection_budget
            connection_budget(config)  # Reject impossible reservations before any persistence.
            if 'youtube_cookie_pool' in data:
                from webapp.cookie_profiles import CookieProfiles
                profiles = CookieProfiles(self.context.config_path.parent, self.context.store)
                for identity in config.get('youtube_cookie_pool', []):
                    profiles.require(identity)
            if self.context.fleet and self.context.fleet.can_download:
                from webapp.fleet import RESOURCE_LIMITS
                limits = {key: config[key] for key in RESOURCE_LIMITS if key in data}
                if limits:
                    self.context.fleet.control(self.context.fleet.id, limits)
            self.context.dependencies.save_settings(self.context.config_path, config)
            self.context.sync_runtime(config, self.context.config_path.parent)
            if any(key in data for key in ('proxy', 'proxy_mode', 'youtube_source_address')):
                self.context.youtube_auth.invalidate()
        message = ('配置已保存；下载与资源策略从后续领取的视频生效，无需暂停或重启。'
                   '配置仅作用于当前实例，正在执行的视频保持本次配置。')
        if 'download_auto_reduce' in data:
            message += '自动降低下载进程上限将在下一次资源采样生效，通常不超过 5 秒。'
        return dict(message=message)
