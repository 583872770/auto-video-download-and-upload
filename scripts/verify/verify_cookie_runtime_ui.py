"""Account cards with real Cookie grouping and isolated admission evidence."""
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from playwright.sync_api import expect, sync_playwright
from werkzeug.serving import make_server, WSGIRequestHandler

from app import create_app
from webapp.browser import browser_options
from webapp.cookie_pool import CookiePool
from webapp.cookie_profiles import CookieProfiles
from webapp.database import cookie_profiles, download_permits, instances, tasks
from webapp.settings import save_settings
from webapp.throughput import policy_key


class QuietHandler(WSGIRequestHandler):
    def log(self, *args, **kwargs):
        pass


def main():
    output = Path(__file__).resolve().parents[2] / '.runtime/cache-archive-20261111/cookie-runtime-preview'
    output.mkdir(parents=True, exist_ok=True)
    environment = {k: v for k, v in os.environ.items() if not k.startswith(('VIDEO_', 'MYSQL_', 'YT5S_'))}
    with tempfile.TemporaryDirectory(prefix='cookie-runtime-') as temporary, patch.dict(os.environ, environment, clear=True):
        root = Path(temporary)
        config_path = root / 'config.yaml'
        config_path.write_text('path: videos\nproxy_mode: direct\nworker_count: 64\nmin_free_gb: 0\n'
                               'builtin_transfer_limit: 12\nbuiltin_connection_limit: 64\n'
                               'download_connection_limit: 64\n', encoding='utf-8')
        app = create_app(config_path, root / 'state', start_worker=False, role='hybrid',
                         instance_id='preview', database_url='sqlite:///' + (root / 'fixture.sqlite3').as_posix())
        store = app.extensions['task_store']
        library = CookieProfiles(root, store)
        data = b'# HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tSID\tFIXTURE\n'
        ids = [library.add(f'account{i + 1:02d}@example.test', data.replace(b'FIXTURE', str(i).encode()))['id']
               for i in range(19)]
        with store.connect() as db:
            db.execute(cookie_profiles.update().where(cookie_profiles.c.id == ids[7]).values(
                content=data.replace(b'FIXTURE', b'0').decode()))
            db.execute(cookie_profiles.update().where(cookie_profiles.c.id == ids[6]).values(content='invalid fixture'))
        local = store.fleet.local_settings()
        config = {**local['config'], 'youtube_cookie_pool': ids, 'youtube_cookie_session_limit': 2,
                  'youtube_cookie': library.require(ids[0]).relative_to(root).as_posix()}
        save_settings(config_path, config)
        store.fleet.save_local(config, local['base'], local['clash_directory'])
        store.fleet.control('preview', {'action': 'resume'})
        pool = CookiePool(store, config, root, None, strict=False)

        def seed(db, stamp):
            for identity, _, scope in pool.accounts:
                state = pool.router._state(db, scope)
                state.update(limit=2, interval=30)
                if identity == ids[3]:
                    state['next_at'] = stamp + 120
                if identity == ids[4]:
                    state.update(cooldown_until=stamp + 600, cooldown_kind='repeated_http_403')
                if identity == ids[5]:
                    state.update(recovery_mode=True, interval=180, next_at=stamp + 180)
                if identity == ids[10]:
                    state['starts'] = [stamp - 60 * i for i in range(30)]
                pool.router._save(db, scope, state)
            gate = pool.router._state(db, pool.global_scope)
            gate['next_at'] = stamp + 90
            pool.router._save(db, pool.global_scope, gate)

        pool.router.transaction_call(seed)
        stages = [(0, 'downloading', True), (0, 'downloading', True), (1, 'waiting_request', True),
                  (2, 'extracting', False), (8, 'processing', False), (None, 'waiting_cookie', False)]
        for index, (account, stage, held) in enumerate(stages):
            job = store.create('download', {'urls': ['https://youtu.be/jNQXAC9IVRw']})
            progress = {'stage': stage, 'title': f'示例视频 {index + 1}', 'speed': 8 * 1024**2,
                        '_reported_at': time.time(), 'last_bytes_at': time.time()}
            if account is not None:
                progress.update(cookie_profile_id=ids[account], cookie_profile_name=f'account{account + 1:02d}@example.test')
            with store.connect() as db:
                db.execute(tasks.update().where(tasks.c.id == job['id']).values(status='running', instance_id='preview',
                    worker_id='fixture', lease_until=time.time() + 600, progress=json.dumps(progress)))
                if account is not None:
                    scope = next(scope for identity, _, scope in pool.accounts if identity == ids[account])
                    db.execute(download_permits.insert().values(token='account-' + str(index),
                        resource=hashlib.sha256(scope.encode()).hexdigest(),
                        task_id=job['id'], owner='fixture', units=1))
                if held:
                    db.execute(download_permits.insert().values(token=str(index),
                        resource=hashlib.sha256(b'local-transfers:preview').hexdigest(),
                        task_id=job['id'], owner='fixture', units=1))

        def beat():
            store.fleet.beat(throughput={'time': time.time(), 'policy': policy_key(config),
                                        'cookie_pool': pool.status()['capacity_summary']})

        beat()
        server = make_server('127.0.0.1', 0, app, threaded=True, request_handler=QuietHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        errors = []
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True, **browser_options(pw.chromium))
                page = browser.new_page(viewport={'width': 1440, 'height': 1100}, device_scale_factor=1)
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.goto(f'http://127.0.0.1:{server.server_port}/#overview')
                panel = page.locator('[data-concurrency-panel=overview]')
                cards = panel.locator('.cookie-runtime-card')
                expect(cards).to_have_count(19)
                expect(panel.locator('.transfer-slot')).to_have_count(0)
                expect(panel.locator('.transfer-effective')).to_contain_text('12')
                card = lambda index: panel.locator(f'[data-cookie-id="{ids[index]}"]')
                expect(card(0).locator('.cookie-task')).to_have_count(2)
                expect(card(1)).to_contain_text('等待请求间隔')
                expect(card(2)).to_contain_text('解析视频')
                expect(card(2)).to_contain_text('未占用传输名额')
                expect(card(3)).to_contain_text('等待两次任务启动最小间隔')
                expect(card(4)).to_contain_text('账号冷却中')
                expect(card(5)).to_contain_text('低频恢复中')
                expect(panel).to_contain_text('至少 300 秒间隔恢复')
                expect(panel).not_to_contain_text('连续成功 3 次')
                expect(card(6)).to_contain_text('Cookie 不可用')
                expect(card(7)).to_contain_text('共用账号额度')
                expect(card(9)).to_contain_text('等待账号池启动间隔')
                expect(card(10)).to_contain_text('账号滚动启动预算已用完')
                expect(card(10)).to_contain_text('小时启动 30 / 30')
                expect(panel.locator('.cookie-unassigned-tasks')).to_contain_text('等待 Cookie')
                panel.screenshot(path=str(output / 'accounts-desktop.png'))
                button = card(0).locator('.cookie-task').first
                button.focus()
                task_id = button.get_attribute('data-task')
                page.evaluate('renderConcurrency()')
                assert page.evaluate('document.activeElement.dataset.task') == task_id
                button.click()
                expect(page.locator('#task-dialog')).to_be_visible()
                page.locator('#task-dialog .dialog-heading [data-close]').click()
                page.set_viewport_size({'width': 390, 'height': 844})
                page.locator('#sidebar').evaluate('node => Promise.all(node.getAnimations().map(a => a.finished))')
                panel.screenshot(path=str(output / 'accounts-mobile.png'))
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), 'horizontal overflow'
                # Keep old observations visibly unknown after losing contact with the instance.
                with store.connect() as db:
                    db.execute(instances.update().where(instances.c.id == 'preview').values(heartbeat=time.time() - 100))
                expect(panel.locator('.cookie-runtime-card.unknown')).to_have_count(19, timeout=12000)
                expect(panel.locator('.cookie-task')).to_have_count(0)
                expect(panel.locator('.transfer-effective')).to_contain_text('—')
                beat()
                expect(card(0).locator('.cookie-task')).to_have_count(2, timeout=12000)
                # Untrusted account names and task titles remain plain text.
                html = page.evaluate("""() => cookieAccountsHTML({online:true,time:Date.now()/1000,
                    cookie_pool:{known:true},transfer_allocation:{configured:12,occupied:[],unoccupied:12,restricted:0,effective:12},
                    cookie_allocation:{accounts:[{id:'a',name:'<script>bad</script>',known:true,state:'ready',
                      tasks:[{task_id:'x',stage:'downloading',title:'<img src=x onerror=alert(1)>',speed:100}],
                      reasons:[{code:'test',label:'<img src=x>'}]}]}})""")
                assert '<script>' not in html and '<img ' not in html
                assert '&lt;script&gt;' in html and '&lt;img ' in html
                # The shared interval is a saved instance setting, independent
                # of the observed per-account interval and existing deadlines.
                page.set_viewport_size({'width': 1440, 'height': 1100})
                page.goto(f'http://127.0.0.1:{server.server_port}/#accounts')
                page.locator('[data-account-tab=pool]').click()
                policy = page.locator('.settings-policy-details')
                if not policy.evaluate('node => node.open'):
                    policy.locator('summary').click()
                field = page.locator('[name="youtube_cookie_pool_start_interval"]')
                runtime = page.locator('[data-youtube-pacing-runtime]')
                expect(field).to_have_value('5')
                expect(runtime).to_contain_text('已保存的账号池共用启动间隔：5 秒')
                expect(runtime).to_contain_text('当前会话采样最短 120 秒')
                gate_before = pool.router.transaction_call(lambda db, stamp: pool.router._state(db, pool.global_scope))
                for value in ('3', '0'):
                    field.fill(value)
                    expect(runtime).to_contain_text('请求节奏有待确认的修改')
                    expect(runtime).to_contain_text('已保存的账号池共用启动间隔：' + ('5 秒' if value == '3' else '3 秒'))
                    page.locator('#account-pool-form button[type="submit"]').click()
                    expect(page.locator('#account-pool-save-state')).to_contain_text('账号池与节奏已保存')
                    saved = page.request.get(f'http://127.0.0.1:{server.server_port}/api/config').json()['config']
                    assert saved['youtube_cookie_pool_start_interval'] == int(value)
                    assert saved['youtube_cookie_pool'] == ids
                    assert saved['youtube_start_interval'] == config['youtube_start_interval']
                    config.update(saved)
                    beat()
                    page.reload()
                    page.locator('[data-account-tab=pool]').click()
                    if not policy.evaluate('node => node.open'):
                        policy.locator('summary').click()
                    expect(field).to_have_value(value)
                    expect(runtime).to_contain_text('已保存的账号池共用启动间隔：' + ('3 秒' if value == '3' else '已关闭'))
                    expect(runtime).not_to_contain_text('请求节奏有待确认的修改')
                assert pool.router.transaction_call(lambda db, stamp: pool.router._state(db, pool.global_scope)) == gate_before
                for width, name in ((1440, 'shared-interval-desktop'), (390, 'shared-interval-mobile')):
                    page.set_viewport_size({'width': width, 'height': 1000})
                    page.locator('#sidebar').evaluate('node => Promise.all(node.getAnimations().map(a => a.finished))')
                    field.scroll_into_view_if_needed()
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), name
                    page.screenshot(path=str(output / f'{name}.png'))
                assert not errors, errors
                browser.close()
        finally:
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()
            store.db.engine.dispose()
        print(json.dumps({'cookie_cards': 19, 'page_errors': errors, 'output': str(output)}, ensure_ascii=False))


if __name__ == '__main__':
    main()
