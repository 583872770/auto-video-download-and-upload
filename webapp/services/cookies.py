"""CookieService operations independent of route registration."""
from Utils.config import resolve_path
from webapp.youtube_login import LoginBusy


class CookieService:
    def __init__(self, context):
        self.context = context

    def cookie_update_busy(self):
        return self.context.store.download_connection_busy()

    def install_browser_cookie(self, data, *, account_name=None, account=None):
        if not account:
            raise ValueError('请从账号清单选择账号登录。')
        with self.context.media_lock, self.context.settings_lock:
            from webapp.account_login import install
            config, base = self.context.settings()
            return install(self.context.store, base, config, data, account, account_name)

    def require_login_idle(self):
        if self.context.app.extensions['youtube_login'].status()['active']:
            raise LoginBusy('请先完成或取消 YouTube 登录，再执行此操作。')

    def start_browser_login(self, identity=None, data=None, actor=None):
        if not identity:
            raise ValueError('请先新增或导入账号，再从账号清单登录。')
        with self.context.media_lock:
            self.require_login_idle()
            config, base = self.context.settings()
            account, credentials = None, None
            if identity:
                from webapp.account_login import prepare
                account, credentials = prepare(self.context.store, base, config, identity, data, actor)
            mode = config.get('proxy_mode', 'manual')
            connection = '直连'
            proxy = ''
            if mode == 'clash':
                snapshot = self.context.clash.snapshot()
                proxy = snapshot['proxy']
                connection = 'Clash · ' + snapshot['node']
            elif mode == 'manual' and config.get('proxy'):
                proxy = config['proxy']
                connection = '手动代理'
            executable = str(resolve_path(base, config['browser_executable'])) if config.get('browser_executable') else ''
            options = {'account': account, 'credentials': credentials} if account else {}
            return self.context.app.extensions['youtube_login'].start({'proxy': proxy, 'browser_executable': executable}, connection, **options)

    def list_youtube_cookies(self):
        from webapp.cookie_profiles import CookieProfiles
        from webapp.youtube_cookie_history import list_events
        with self.context.settings_lock:
            config, base = self.context.settings()
            profiles = CookieProfiles(base, self.context.store)
            items = profiles.items()
            for item in items:
                item['auth'] = self.context.youtube_auth.profile_status(item['id'], base)
                item['pooled'] = item['id'] in config.get('youtube_cookie_pool', [])
            pool_status = None
            if config.get('youtube_cookie_pool'):
                from webapp.cookie_pool import CookiePool
                pool_status = CookiePool(self.context.store, config, base, None, strict=False).status()
            sessions = {row['id']: row for row in (pool_status or {}).get('session_states', [])}
            for item in items:
                item['session'] = sessions.get(item['id'])
                recent = list_events(self.context.store, item['id'], limit=1)
                item['latest_event'] = recent[0] if recent else None
        return dict(items=items, pool=pool_status)

    def check_youtube_cookie_profile(self, identity):
        with self.context.media_lock:
            with self.context.settings_lock:
                self.require_login_idle()
                config, base = self.context.settings()
                options = {}
                if config.get('proxy_mode') == 'clash':
                    snapshot = self.context.clash.snapshot()
                    options.update(proxy=snapshot['proxy'], node=snapshot['node'])
            result = self.context.youtube_auth.check_profile(identity, config, base, recover=True, **options)
        return dict(auth=result, message=result['message'])


    def delete_youtube_cookie(self, identity):
        from webapp.cookie_profiles import CookieProfiles
        with self.context.media_lock, self.context.settings_lock:
            config, base = self.context.settings()
            profiles = CookieProfiles(base, self.context.store)
            items = profiles.items()
            item = next((item for item in items if item['id'] == identity), None)
            if item is None:
                raise ValueError('Cookie 配置不存在。')
            pooled = identity in config.get('youtube_cookie_pool', [])
            changed = pooled
            previous = dict(config)
            original = self.context.config_path.read_bytes()
            if changed:
                self.require_login_idle()
                if self.cookie_update_busy():
                    raise LoginBusy('请停止实例并等待下载结束后，再删除此 Cookie。')
                config['youtube_cookie_pool'] = [value for value in config['youtube_cookie_pool'] if value != identity]
            try:
                if changed:
                    self.context.dependencies.save_settings(self.context.config_path, config)
                    self.context.sync_runtime(config, base)
                    self.context.youtube_auth.invalidate(preserve_block=True)
                profiles.delete(identity)
            except Exception:
                if changed:
                    self.context.config_path.write_bytes(original)
                    self.context.sync_runtime(previous, base)
                raise
        return dict(message='Cookie 已删除。', cookie_pool=config.get('youtube_cookie_pool', []))
