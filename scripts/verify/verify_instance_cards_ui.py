"""Real browser verification of mode-specific instance controls and isolation."""
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
from webapp.cookie_profiles import CookieProfiles
from webapp.database import instances
from webapp.settings import save_settings


class QuietHandler(WSGIRequestHandler):
    def log(self, *args, **kwargs):
        pass


def main():
    output = Path(__file__).resolve().parents[2] / '.runtime/cache-archive-20261111/instance-cards-preview'
    output.mkdir(parents=True, exist_ok=True)
    environment = {k: v for k, v in os.environ.items() if not k.startswith(('VIDEO_', 'MYSQL_', 'YT5S_'))}
    with tempfile.TemporaryDirectory(prefix='instance-cards-') as temporary, patch.dict(os.environ, environment, clear=True):
        root = Path(temporary)
        apps, servers, threads = {}, [], []
        for node, mode in [('pool', 'manual'), ('manual', 'manual'), ('auto', 'auto')]:
            base = root / node
            base.mkdir()
            config_path = base / 'config.yaml'
            config_path.write_text(f'path: videos\nproxy_mode: direct\nworker_count: 12\nmin_free_gb: 0\n'
                                   f'throughput_mode: {mode}\ndownload_methods: [yt_dlp]\nbuiltin_transfer_limit: 12\n', encoding='utf-8')
            app = create_app(config_path, base / 'state', start_worker=False, role='hybrid',
                             instance_id=node, instance_name=node, database_url='sqlite:///' + (root / 'shared.sqlite3').as_posix())
            apps[node] = app
            if node == 'pool':
                store = app.extensions['task_store']
                library = CookieProfiles(base, store)
                cookie = b'# HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tSID\tFIXTURE\n'
                ids = [library.add(f'Account {n}', cookie.replace(b'FIXTURE', str(n).encode()))['id'] for n in range(3)]
                local = store.fleet.local_settings()
                config = {**local['config'], 'youtube_cookie_pool': ids,
                          'youtube_cookie': library.require(ids[0]).relative_to(base).as_posix()}
                save_settings(config_path, config)
                store.fleet.save_local(config, local['base'], local['clash_directory'])
            server = make_server('127.0.0.1', 0, app, threaded=True, request_handler=QuietHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            servers.append(server)
            threads.append(thread)
            store = app.extensions['task_store']
            with store.connect() as db:
                db.execute(instances.update().where(instances.c.id == node).values(url=f'http://127.0.0.1:{server.server_port}'))
                from webapp.runtime_summary import record_terminal
                record_terminal(store, db, {'action': 'download', 'instance_id': node}, 'succeeded',
                    {}, {'file_count': 1, 'file_bytes': 12345 if node == 'pool' else 99999}, time.time())
        errors = []
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True)
                page = browser.new_page(viewport={'width': 1440, 'height': 1100})
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.goto(f'http://127.0.0.1:{servers[0].server_port}/#instances')
                expect(page.locator('.fleet-instance')).to_have_count(3)
                pool = page.locator('[data-instance-form="pool"]')
                manual = page.locator('[data-instance-form="manual"]')
                auto = page.locator('[data-instance-form="auto"]')
                expect(pool).to_contain_text('本实例 Cookie 池 · 3 份')
                expect(pool.locator('[name="worker_count"]')).to_have_count(0)
                expect(pool.locator('[name="yt5s_browser_count"]')).to_have_count(0)
                expect(pool.locator('[name="builtin_transfer_limit"]')).to_have_value('12')
                expect(manual.locator('[name="worker_count"]')).to_have_value('12')
                expect(manual).to_contain_text('视频处理并发上限')
                expect(auto).to_contain_text('自动调度传输上限')
                expect(page.locator('#instances-view .cookie-runtime-card')).to_have_count(0)
                expect(page.locator('#fleet-job-context')).to_have_count(0)
                pool.locator('summary').click()
                pool.locator('[name="builtin_transfer_limit"]').fill('9')
                pool.locator('[name="youtube_cookie_session_limit"]').fill('3')
                pool.locator('[name="youtube_cookie_pool_start_interval"]').fill('6')
                pool.locator('[name="youtube_cookie_pool_worker_limit"]').fill('7')
                page.wait_for_timeout(2300)
                expect(pool.locator('[name="builtin_transfer_limit"]')).to_have_value('9')
                pool.locator('button[type="submit"]').click()
                expect(page.locator('.toast').last).to_contain_text('实例配置已保存')
                local = apps['pool'].extensions['fleet'].local_settings()['config']
                assert [local[k] for k in ('builtin_transfer_limit', 'youtube_cookie_session_limit', 'youtube_cookie_pool_start_interval')] == [9, 3, 6]
                assert local['youtube_cookie_pool_worker_limit'] == 7
                assert apps['manual'].extensions['fleet'].local_settings()['config']['builtin_transfer_limit'] == 12
                # Opening the complete settings page reads the exact same values.
                pool.get_by_role('link', name='Cookie 与完整设置').click()
                page.locator('#settings-tab-accounts').click()
                expect(page.locator('[name="youtube_cookie_pool_start_interval"]')).to_have_value('6')
                expect(page.locator('#settings-form [name="youtube_cookie_pool_worker_limit"]')).to_have_value('7')
                expect(page.locator('#settings-form [name="youtube_cookie_pool_preparation_workers"]')).to_have_value('4')
                page.locator('#settings-tab-storage').click()
                page.locator('#runtime-summary-open').click()
                expect(page.locator('#runtime-summary-content')).to_contain_text('实例：pool')
                expect(page.locator('#runtime-summary-content')).to_contain_text('完成 1 个')
                page.locator('#runtime-summary-days').select_option('30')
                expect(page.locator('#runtime-summary-content')).to_contain_text('实例：pool')
                page.screenshot(path=str(output / 'runtime-summary-desktop.png'), full_page=True)
                page.set_viewport_size({'width': 390, 'height': 844})
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), 'summary page overflow'
                assert page.locator('#runtime-summary-content').evaluate('(element) => element.scrollWidth <= element.clientWidth'), 'summary text overflow'
                page.screenshot(path=str(output / 'runtime-summary-mobile.png'), full_page=True)
                page.locator('#runtime-summary-close').click()
                page.set_viewport_size({'width': 1440, 'height': 1100})
                page.goto(f'http://127.0.0.1:{servers[0].server_port}/#instances')
                expect(page.locator('.fleet-instance')).to_have_count(3)
                page.screenshot(path=str(output / 'instances-desktop.png'), full_page=True)
                # Offline metrics must not look like a fresh successful observation.
                with apps['pool'].extensions['task_store'].connect() as db:
                    db.execute(instances.update().where(instances.c.id == 'manual').values(heartbeat=time.time()-100))
                expect(page.locator('[data-instance-state="manual"]')).to_have_text('离线', timeout=12000)
                expect(page.locator('[data-instance-live="manual"]')).to_contain_text('—')
                page.set_viewport_size({'width': 390, 'height': 844})
                expect(page.locator('#sidebar')).not_to_be_in_viewport()
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), 'horizontal overflow'
                page.screenshot(path=str(output / 'instances-mobile.png'), full_page=True)
                assert not errors, errors
                browser.close()
        finally:
            for server in servers:
                server.shutdown()
            for thread in threads:
                thread.join(timeout=3)
    print('PASS: pool/manual/auto, process controls, scoped runtime summary, drafts, offline state, desktop/mobile')


if __name__ == '__main__':
    main()
