"""Isolated manager/worker browser checks; never connect to the configured MySQL."""
from pathlib import Path
import argparse
import sys
import tempfile
import threading
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from playwright.sync_api import expect, sync_playwright
from sqlalchemy import update
from werkzeug.serving import make_server

from app import create_app
from scripts.verify_web import QuietHandler
from Utils.runtime import ROOT
from webapp.browser import browser_options
from webapp.database import instances


def main(hybrid=False):
    output = ROOT / '.cache/validation' / (('hybrid-' if hybrid else 'fleet-') + uuid.uuid4().hex[:10])
    output.mkdir(parents=True)
    servers = []
    errors = []
    with tempfile.TemporaryDirectory(prefix='frameflow-fleet-ui-') as temporary:
        root = Path(temporary)
        url = 'sqlite:///' + (root / 'shared.sqlite3').as_posix()
        apps = {}
        try:
            download_role = 'hybrid' if hybrid else 'worker'
            for identity, role in [('manager', 'manager'), ('alpha', download_role), ('beta', download_role)]:
                base = root / identity
                base.mkdir()
                config = base / 'config.yaml'
                config.write_text(f'path: videos-{identity}\nproxy_mode: direct\nmin_free_gb: 0\n'
                                  'download_method: downmp3\n', encoding='utf-8')
                app = create_app(config, base / 'state', start_worker=False, role=role,
                                 instance_id=identity, instance_name=identity, database_url=url)
                server = make_server('127.0.0.1', 0, app, threaded=True, request_handler=QuietHandler)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                servers.append((server, thread))
                address = f'http://127.0.0.1:{server.server_port}'
                fleet = app.extensions['fleet']
                with fleet.store.connect() as db:
                    db.execute(update(instances).where(instances.c.id == identity).values(url=address))
                apps[identity] = app
                if role == 'manager':
                    manager_address = address
                else:
                    media = base / f'videos-{identity}'
                    media.mkdir()
                    (media / f'{identity}.mp4').write_bytes(b'video-fixture' * 100)
                    app.extensions['media_catalog'].reindex(media)
            apps['manager'].extensions['task_store'].set_downloads_paused(True)
            store = apps['manager'].extensions['task_store']
            with store.connect() as db:
                db.execute(update(instances).where(instances.c.role == download_role).values(role='pending'))
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True, **browser_options(pw.chromium))
                try:
                    page = browser.new_page(viewport={'width': 1440, 'height': 1080}, reduced_motion='reduce')
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    page.goto(manager_address + '/#instances', wait_until='networkidle')
                    expect(page.locator('#fleet-nav')).to_be_visible()
                    expect(page.locator('[data-instance-form]')).to_have_count(0)
                    with store.connect() as db:
                        db.execute(update(instances).where(instances.c.role == 'pending').values(role=download_role))
                    expect(page.locator('#settings-form [name=path]')).to_have_value('videos-alpha', timeout=10000)
                    expect(page.locator('[data-instance-form]')).to_have_count(2)
                    expect(page.locator('[data-instance-form="alpha"]')).to_contain_text('累计成功')
                    expect(page.locator('#fleet-selection')).to_have_value('alpha')
                    alpha = page.locator('[data-instance-form="alpha"]')
                    alpha.locator('[name=worker_count]').fill('2')
                    alpha.locator('button[type=submit]').click()
                    expect(page.locator('#toasts')).to_contain_text('实例配置已保存')
                    assert apps['alpha'].extensions['task_store'].runtime()['worker_count'] == 2
                    page.locator('#fleet-global-limit').fill('4')
                    page.locator('#fleet-global-form button').click()
                    expect(page.locator('#toasts')).to_contain_text('全局并发上限已保存')
                    page.screenshot(path=str(output / 'instances-desktop.png'), full_page=True)
                    page.locator('.nav-item[data-view=settings]').click()
                    expect(page.locator('#settings-form [name=path]')).to_have_value('videos-alpha')
                    page.locator('#fleet-selection').select_option('beta')
                    expect(page.locator('#settings-form [name=path]')).to_have_value('videos-beta')
                    page.locator('.nav-item[data-view=media]').click()
                    expect(page.locator('#all-media .media-card')).to_have_count(1)
                    expect(page.locator('#all-media')).to_contain_text('beta')
                    page.locator('#all-media .media-card').click()
                    expect(page.locator('#media-dialog')).to_be_visible()
                    expect(page.locator('#media-download')).to_have_attribute('href', '/media/videos/beta.mp4?instance_id=beta')
                    page.locator('#media-dialog [data-close]').click()
                    page.locator('#clear-media').click()
                    expect(page.locator('#media-clear-dialog')).to_be_visible()
                    expect(page.locator('#media-clear-directory')).to_contain_text('videos-beta')
                    expect(page.locator('#media-clear-submit')).to_be_disabled()
                    page.locator('#media-clear-dialog [data-close]').first.click()
                    page.locator('.nav-item[data-view=instances]').click()
                    page.set_viewport_size({'width': 390, 'height': 844})
                    expect(page.locator('#fleet-selection')).to_be_visible()
                    assert page.evaluate('() => document.documentElement.scrollWidth <= window.innerWidth'), 'mobile overflow'
                    page.screenshot(path=str(output / 'instances-mobile.png'), full_page=True)
                    if hybrid:
                        for identity in ('alpha', 'beta'):
                            with store.connect() as db:
                                address = db.execute(instances.select().where(instances.c.id == identity)).mappings().one()['url']
                            page.set_viewport_size({'width': 1440, 'height': 1080})
                            page.goto(address + '/#instances', wait_until='networkidle')
                            expect(page.locator('#fleet-selection')).to_have_value(identity)
                            expect(page.locator('[data-instance-form]')).to_have_count(2)
                            expect(page.locator(f'[data-instance-form="{identity}"] h2')).to_contain_text('本机')
                            page.locator('.nav-item[data-view=settings]').click()
                            expect(page.locator('#settings-form [name=path]')).to_have_value('videos-' + identity)
                            other = 'beta' if identity == 'alpha' else 'alpha'
                            page.locator('#fleet-selection').select_option(other)
                            expect(page.locator('#settings-form [name=path]')).to_have_value('videos-' + other)
                            page.locator('#fleet-selection').select_option(identity)
                            page.locator('.nav-item[data-view=instances]').click()
                            page.screenshot(path=str(output / (identity + '-desktop.png')), full_page=True)
                            page.set_viewport_size({'width': 390, 'height': 844})
                            assert page.evaluate('() => document.documentElement.scrollWidth <= window.innerWidth'), 'hybrid mobile overflow'
                            page.screenshot(path=str(output / (identity + '-mobile.png')), full_page=True)
                            page.goto(address + '/jobs', wait_until='networkidle')
                            expect(page.locator('body')).to_contain_text('任务中心')
                    # An authenticated manager must keep its session when a node URL is wrong.
                    page.set_viewport_size({'width':1440,'height':1080})
                    page.goto(manager_address + '/login')
                    page.locator('[name=bootstrap_key]').fill(apps['manager'].extensions['console_auth'].bootstrap_key)
                    page.locator('#login-form [name=username]').fill('admin')
                    page.locator('#login-form [name=password]').fill('fixture-browser-password')
                    page.locator('#login-form button').click()
                    expect(page.locator('#center-view')).to_be_visible()
                    with store.connect() as db:
                        db.execute(update(instances).where(instances.c.id == 'alpha').values(url=manager_address))
                    page.evaluate('localStorage.setItem("frameflow-instance:" + location.origin, "alpha")')
                    page.goto(manager_address + '/#overview')
                    expect(page.locator('#overview-view')).to_be_visible()
                    expect(page.locator('#connection-warning')).to_contain_text('拒绝了实例认证')
                    expect(page).to_have_url(manager_address + '/#overview')
                    assert page.request.get(manager_address + '/api/auth/status').json()['user']['username'] == 'admin'
                    assert page.request.get(manager_address + '/api/overview').ok
                    page.screenshot(path=str(output / 'node-auth-error-keeps-management-login.png'), full_page=True)
                    # Reproduce the cross-machine localhost registration seen in the real deployment.
                    with store.connect() as db:
                        db.execute(update(instances).where(instances.c.id == 'alpha').values(host='other-machine', url='http://127.0.0.1:5000'))
                    page.reload()
                    expect(page.locator('#connection-warning')).to_contain_text('本机回环地址')
                    expect(page).to_have_url(manager_address + '/#overview')
                    page.goto(manager_address + '/jobs')
                    expect(page.locator('#center-view')).to_be_visible()
                    assert not errors, errors
                finally:
                    browser.close()
        finally:
            for server, thread in reversed(servers):
                server.shutdown()
                server.server_close()
                thread.join(timeout=3)
    print('Fleet UI checks passed:', output)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--hybrid', action='store_true')
    main(parser.parse_args().hybrid)
