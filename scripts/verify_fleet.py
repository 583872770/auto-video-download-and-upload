"""Isolated manager/worker browser checks; never connect to the configured MySQL."""
from pathlib import Path
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


def main():
    output = ROOT / '.cache/validation' / ('fleet-' + uuid.uuid4().hex[:10])
    output.mkdir(parents=True)
    servers = []
    errors = []
    with tempfile.TemporaryDirectory(prefix='frameflow-fleet-ui-') as temporary:
        root = Path(temporary)
        url = 'sqlite:///' + (root / 'shared.sqlite3').as_posix()
        apps = {}
        try:
            for identity, role in [('manager', 'manager'), ('alpha', 'worker'), ('beta', 'worker')]:
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
                db.execute(update(instances).where(instances.c.role == 'worker').values(role='pending'))
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True, **browser_options(pw.chromium))
                try:
                    page = browser.new_page(viewport={'width': 1440, 'height': 1080}, reduced_motion='reduce')
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    page.goto(manager_address + '/#instances', wait_until='networkidle')
                    expect(page.locator('#fleet-nav')).to_be_visible()
                    expect(page.locator('[data-instance-form]')).to_have_count(0)
                    with store.connect() as db:
                        db.execute(update(instances).where(instances.c.role == 'pending').values(role='worker'))
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
    main()
