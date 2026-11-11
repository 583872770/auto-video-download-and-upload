"""CookieService operations independent of route registration."""
from pathlib import Path
import shutil
import tempfile
import time
from Utils.config import resolve_path
from Utils.youtube_login import parse_youtube_cookie_file, save_youtube_cookies
from webapp.youtube_login import LoginBusy, browser_cookie_name


class CookieService:
    def __init__(self, context):
        self.context = context

    def cookie_update_busy(self):
        return self.context.store.download_connection_busy()

    def install_cookie(self, data):
        cookies = parse_youtube_cookie_file(data)
        with self.context.media_lock, self.context.settings_lock:
            if self.cookie_update_busy():
                raise LoginBusy('请先停止全部下载并等待当前任务结束，再更新 Cookie。')
            config, base = self.context.settings()
            target = base / 'Cookies' / 'youtube.txt'
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists() and not target.is_file():
                raise ValueError('Cookie 保存位置不是文件，请检查 Cookies/youtube.txt。')
            with tempfile.TemporaryDirectory(prefix='.youtube-import-', dir=target.parent) as staging:
                backup = Path(staging) / 'previous.txt'
                config_backup = Path(staging) / 'config.yaml'
                shutil.copy2(self.context.config_path, config_backup)
                if target.is_file():
                    shutil.copy2(target, backup)
                save_youtube_cookies(cookies, target)
                try:
                    config['youtube_cookie'] = 'Cookies/youtube.txt'
                    self.context.dependencies.save_settings(self.context.config_path, config)
                    self.context.sync_runtime(config, base)
                    from webapp.cookie_profiles import CookieProfiles
                    CookieProfiles(base, self.context.store).save_legacy(target.read_bytes())
                    self.context.youtube_auth.invalidate()
                except Exception:
                    config_backup.replace(self.context.config_path)
                    if backup.is_file():
                        backup.replace(target)
                    else:
                        target.unlink(missing_ok=True)
                    raise

    def install_browser_cookie(self, data, *, account_name=None, account=None):
        with self.context.media_lock, self.context.settings_lock:
            if account:
                from webapp.account_login import install
                config, base = self.context.settings()
                return install(self.context.store, base, config, data, account, account_name)
            from webapp.cookie_profiles import CookieProfiles
            config, base = self.context.settings()
            profiles = CookieProfiles(base, self.context.store)
            item = profiles.add(browser_cookie_name(account_name), data)
            name = item['name']
            if self.cookie_update_busy():
                return f'YouTube 登录成功，已新增 Cookie「{name}」。当前下载继续使用原 Cookie；停止当前实例后可在列表中选择新 Cookie。'
            original_config = self.context.config_path.read_bytes()
            try:
                config['youtube_cookie'] = profiles.require(item['id']).relative_to(base).as_posix()
                self.context.dependencies.save_settings(self.context.config_path, config)
                self.context.sync_runtime(config, base)
                self.context.youtube_auth.invalidate()
            except Exception:
                self.context.config_path.write_bytes(original_config)
                profiles.delete(item['id'])
                raise
        return f'YouTube 登录成功，已新增并启用 Cookie「{name}」，后续下载将自动使用。'

    def require_login_idle(self):
        if self.context.app.extensions['youtube_login'].status()['active']:
            raise LoginBusy('请先完成或取消 YouTube 登录，再执行此操作。')

    def start_browser_login(self, identity=None, data=None, actor=None):
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
            # Repair installations created before first-upload auto-activation:
            # a saved profile with no selected path is safe to make active.
            if not config.get('youtube_cookie'):
                saved = profiles.items()
                from webapp.cookie_binding import CookieBindingConflict
                for item in saved:
                    try:
                        target = profiles.require(item['id'])
                    except CookieBindingConflict:
                        continue  # Keep legacy conflicts visible for removal/export.
                    config['youtube_cookie'] = target.relative_to(base).as_posix()
                    config['youtube_cookie_pool'] = []
                    self.context.dependencies.save_settings(self.context.config_path, config)
                    self.context.sync_runtime(config, base)
                    self.context.youtube_auth.invalidate()
                    break
            items = profiles.items(config.get('youtube_cookie', ''))
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
        return dict(items=items, pool=pool_status, cookie_path=config.get('youtube_cookie', ''),
                       custom_active=bool(config.get('youtube_cookie') and not any(item['active'] for item in items)))

    def check_youtube_cookie_profile(self, identity):
        with self.context.media_lock, self.context.settings_lock:
            self.require_login_idle()
            config, base = self.context.settings()
            options = {}
            if config.get('proxy_mode') == 'clash':
                snapshot = self.context.clash.snapshot()
                options.update(proxy=snapshot['proxy'], node=snapshot['node'])
            result = self.context.youtube_auth.check_profile(identity, config, base, recover=True, **options)
        return dict(auth=result, message=result['message'])

    def recover_youtube_cookie_pool(self):
        from webapp.cookie_pool import CookiePool
        with self.context.media_lock, self.context.settings_lock:
            self.require_login_idle()
            if self.cookie_update_busy() or not self.context.store.claims_paused():
                raise LoginBusy('请先停止当前实例，并等待全部下载结束，再检查账号池。')
            config, base = self.context.settings()
            if not config.get('youtube_cookie_pool'):
                raise ValueError('当前未启用 Cookie 账号池。')
            pool = CookiePool(self.context.store, config, base, None)
            if pool.status().get('cooldown_until', 0) > time.time():
                raise ValueError('账号池仍在冷却，请等待冷却结束后再检测和解除拦截。')
            options = {}
            if config.get('proxy_mode') == 'clash':
                snapshot = self.context.clash.snapshot()
                options.update(proxy=snapshot['proxy'], node=snapshot['node'])
            for identity in config['youtube_cookie_pool']:
                result = self.context.youtube_auth.check_profile(identity, config, base, force=True, **options)
                if result['state'] != 'valid':
                    return dict(error='账号池登录检查未全部通过，拦截保持有效。', auth=result), 409
            pool.clear_halt_after_checks()
        return dict(message='账号池已通过检查并解除拦截，会话重新从 1 个任务预热，池独立调度。请手动恢复下载；源站冷却保持有效。')

    def select_youtube_cookie(self, identity):
        from webapp.cookie_profiles import CookieProfiles
        with self.context.media_lock, self.context.settings_lock:
            self.require_login_idle()
            if self.cookie_update_busy():
                raise LoginBusy('请先停止当前实例并等待全部下载结束，再切换 Cookie。')
            config, base = self.context.settings()
            target = CookieProfiles(base, self.context.store).require(identity)
            parse_youtube_cookie_file(target.read_bytes())
            previous = dict(config)
            original = self.context.config_path.read_bytes()
            config['youtube_cookie'] = target.relative_to(base).as_posix()
            config['youtube_cookie_pool'] = []
            try:
                self.context.dependencies.save_settings(self.context.config_path, config)
                self.context.sync_runtime(config, base)
            except Exception:
                self.context.config_path.write_bytes(original)
                self.context.sync_runtime(previous, base)
                raise
            self.context.youtube_auth.invalidate(preserve_block=True)
        return dict(cookie_path=config['youtube_cookie'], message='已切换为单 Cookie，账号池已关闭。请检查登录状态后恢复下载；已有的限流冷却保持有效。')

    def delete_youtube_cookie(self, identity):
        from webapp.cookie_profiles import CookieProfiles
        with self.context.media_lock, self.context.settings_lock:
            config, base = self.context.settings()
            profiles = CookieProfiles(base, self.context.store)
            items = profiles.items(config.get('youtube_cookie', ''))
            item = next((item for item in items if item['id'] == identity), None)
            if item is None:
                raise ValueError('Cookie 配置不存在。')
            pooled = identity in config.get('youtube_cookie_pool', [])
            changed = item['active'] or pooled
            replacement = None
            previous = dict(config)
            original = self.context.config_path.read_bytes()
            if changed:
                self.require_login_idle()
                if self.cookie_update_busy():
                    raise LoginBusy('请先停止当前实例并等待全部下载结束，再删除正在使用的 Cookie。')
                if item['active']:
                    replacement = next((value for value in items if value['id'] != identity), None)
                    config['youtube_cookie'] = (profiles.require(replacement['id']).relative_to(base).as_posix()
                                                if replacement else '')
                if pooled:
                    remaining_pool = [value for value in config['youtube_cookie_pool'] if value != identity]
                    config['youtube_cookie_pool'] = remaining_pool if len(remaining_pool) >= 2 else []
            try:
                if changed:
                    self.context.dependencies.save_settings(self.context.config_path, config)
                    self.context.sync_runtime(config, base)
                    self.context.youtube_auth.invalidate(preserve_block=True)
                profiles.delete(identity)
            except Exception:
                if changed:
                    self.context.config_path.write_bytes(original)
                    profiles.restore_selected(previous.get('youtube_cookie', ''))
                    self.context.sync_runtime(previous, base)
                raise
        message = ('Cookie 已删除，已自动选择其他 Cookie 作为当前使用项。' if replacement else
                   'Cookie 已删除，当前未使用 Cookie。' if item['active'] else 'Cookie 已删除。')
        return dict(message=message, cookie_path=config.get('youtube_cookie', ''),
                       cookie_pool=config.get('youtube_cookie_pool', []),
                       selection_changed=bool(item['active']))

    def add_youtube_cookie(self, data, name, filename, activate=True):
        context = self.context
        from webapp.cookie_profiles import CookieProfiles
        with context.media_lock, context.settings_lock:
            config, base = context.settings()
            item = CookieProfiles(base, context.store).add(name, data, filename)
            # A first upload must be immediately usable. Keep an explicitly
            # selected Cookie untouched when adding a second profile.
            activated = activate and not bool(config.get('youtube_cookie'))
            if activated:
                config['youtube_cookie'] = f"Cookies/profiles/{item['id']}.txt"
                config['youtube_cookie_pool'] = []
                context.dependencies.save_settings(context.config_path, config)
                context.sync_runtime(config, base)
                context.youtube_auth.invalidate()
                item['active'] = True
        message = ('Cookie 已保存并设为当前使用；请点击“检测登录”确认当前出口可用。'
                   if activated else 'Cookie 已保存；请在列表中点击“使用此 Cookie”后生效。')
        return dict(item=item, activated=activated, message=message), 201


