"""Verify zero-limit saves and diagnostics using an isolated local browser fixture."""
from contextlib import ExitStack
import os
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from playwright.sync_api import expect, sync_playwright
from werkzeug.serving import make_server
from app import create_app
from scripts.settings_roundtrip_checks import reveal_setting
from scripts.verify.verify_jobs import QuietHandler
from webapp.browser import browser_options
from webapp.cookie_profiles import CookieProfiles
from webapp.throughput import MIB, ThroughputController
from webapp.concurrency_diagnostics import diagnose


def main():
    output = Path('.runtime/verify/zero-limits')
    output.mkdir(parents=True, exist_ok=True)
    environment = {k:v for k,v in os.environ.items() if not k.startswith(('VIDEO_', 'MYSQL_'))}
    with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, environment, clear=True), ExitStack() as stack:
        root = Path(tmp)
        config = root / 'config.yaml'
        config.write_text('path: videos\nproxy_mode: direct\n', encoding='utf-8')
        app = create_app(config, root/'state', start_worker=False, role='hybrid',
                         instance_id='zero-limits', database_url='sqlite:///'+(root/'test.db').as_posix())
        store = app.extensions['task_store']
        stack.callback(store.db.engine.dispose)
        profiles = CookieProfiles(root, store)
        ids = [profiles.add(str(i), f'# HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tSID\tfixture-{i}\n'.encode())['id'] for i in range(3)]
        initial = dict(youtube_cookie_pool=ids, youtube_cookie=profiles.path(ids[0]).relative_to(root).as_posix(), youtube_cookie_session_limit=2,
                       builtin_transfer_limit=0, download_connection_limit=64)
        client = app.test_client()
        with client.session_transaction() as session:
            session['csrf'] = 'isolated-zero-limit-check'
        r = client.put('/api/config', json=initial, headers={'X-CSRF-Token':'isolated-zero-limit-check'})
        assert r.status_code == 200, r.json
        assert len(client.get('/api/youtube/cookies').json['items']) == 3
        store.fleet.beat()
        server = make_server('127.0.0.1', 0, app, threaded=True, request_handler=QuietHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def shutdown():
            server.shutdown(); server.server_close(); thread.join(timeout=3)
        stack.callback(shutdown)
        errors = []
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, **browser_options(pw.chromium))
            page = browser.new_page(viewport={'width':1440,'height':1050})
            page.on('pageerror', lambda e:errors.append(str(e)))
            base = f'http://127.0.0.1:{server.server_port}'
            page.goto(base+'/#settings')
            form = page.locator('#settings-form')
            limit = reveal_setting(page, form.locator('[name=youtube_cookie_pool_worker_limit]'))
            limit.fill('0')
            expect(page.locator('#database-budget-preview')).to_contain_text('目标 6 个任务进程')
            expect(page.locator('#database-budget-preview')).to_contain_text('需要 23 个')
            with page.expect_response(lambda r:r.request.method=='PUT' and r.url.endswith('/api/config')) as response:
                form.locator('button[type=submit]').click()
            assert response.value.status == 200
            expect(form.locator('button[type=submit]')).to_be_enabled()
            page.reload()
            expect(limit).to_have_value('0')
            assert len(client.get('/api/config').json['config']['youtube_cookie_pool']) == 3
            reveal_setting(page, limit)
            page.screenshot(path=str(output/'process-limit.png'), full_page=True)
            page.goto(base+'/#accounts')
            expect(page.locator('#youtube-cookie-pool-enabled')).to_be_checked()
            expect(page.locator('#youtube-cookie-list [data-cookie-pool-id]:checked')).to_have_count(3)
            page.locator('[data-account-tab=pool]').click()
            pool = page.locator('#account-pool-form')
            for key in ('youtube_hourly_start_limit','youtube_daily_start_limit'):
                field = reveal_setting(page, pool.locator(f'[name={key}]'))
                field.fill('0')
                assert field.evaluate('(el)=>el.checkValidity()')
            with page.expect_response(lambda r:r.request.method=='PUT' and r.url.endswith('/api/config')) as response:
                pool.locator('button[type=submit]').click()
            assert response.value.status == 200
            expect(pool.locator('button[type=submit]')).to_be_enabled()
            page.reload()
            page.locator('[data-account-tab=pool]').click()
            for key in ('youtube_hourly_start_limit','youtube_daily_start_limit'):
                expect(pool.locator(f'[name={key}]')).to_have_value('0')
            saved = app.test_client().get('/api/config').json['config']
            assert all(saved[k]==0 for k in ('youtube_cookie_pool_worker_limit','youtube_hourly_start_limit','youtube_daily_start_limit'))
            controller = ThroughputController(root/'metrics')
            sample = dict(cpu_cores=4, memory_total=16384*MIB, memory_available=10000*MIB,
                          cpu_percent=30, io_pressure=0, rx=0)
            with patch.object(controller,'sample',return_value=sample):
                rate=controller.update(saved,cookie_capacity=dict(known=True,capacity=6,reason='ready'))
            diagnostic=diagnose(saved,'zero-limits',[],rate,({}, {}, {}),time.time())
            rendered=page.evaluate('(data)=>workerControlHTML(data)',diagnostic)
            (output/'diagnostic.json').write_text(json.dumps({'saved':saved,'diagnostic':diagnostic},ensure_ascii=False,default=str),encoding='utf-8')
            (output/'rendered.html').write_text(rendered,encoding='utf-8')
            assert '配置上限 不限制' in rendered and '配置的进程上限 不限制' in rendered
            assert '目标 6 个进程' in rendered and 'NaN' not in rendered
            page.set_viewport_size({'width':390,'height':844})
            reveal_setting(page,pool.locator('[name=youtube_hourly_start_limit]'))
            page.screenshot(path=str(output/'budgets-mobile.png'),full_page=True)
            assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
            assert not errors, errors
            browser.close()
    print('PASS: zero saves/reloads, automatic pool demand, unlimited diagnostics and mobile layout.')


if __name__ == '__main__':
    main()
