"""Verify live strategy edits against isolated services without real downloads."""
import argparse
from pathlib import Path
import sys
import tempfile
import threading
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from playwright.sync_api import expect, sync_playwright
from werkzeug.serving import make_server

from app import create_app
from scripts.verify_web import QuietHandler
from scripts.settings_roundtrip_checks import check_settings_roundtrip
from webapp.browser import browser_options
from webapp.database import instances


def main(hybrid=False):
    output = Path(__file__).resolve().parents[1] / '.cache/validation' / ('hot-policy-' + uuid.uuid4().hex[:10])
    output.mkdir(parents=True)
    servers = []
    heartbeat_stop = threading.Event()
    heartbeat = None
    with tempfile.TemporaryDirectory(prefix='frameflow-hot-policy-') as temporary:
        root = Path(temporary)
        url = 'sqlite:///' + (root / 'shared.sqlite3').as_posix()
        apps, addresses = {}, {}
        try:
            for identity, role in [('manager', 'hybrid' if hybrid else 'manager'), ('worker', 'worker')]:
                base = root / identity
                base.mkdir()
                config = base / 'config.yaml'
                config.write_text('path: videos\nproxy_mode: direct\nmin_free_gb: 0\nworker_count: 3\n'
                    'download_methods: [yt5s, downmp3]\nyt5s_quality: "360"\ndownmp3_quality: "720"\n', encoding='utf-8')
                app = create_app(config, base / 'state', start_worker=False, role=role,
                                 instance_id=identity, database_url=url)
                apps[identity] = app
                server = make_server('127.0.0.1', 0, app, threaded=True, request_handler=QuietHandler)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                servers.append((server, thread))
                addresses[identity] = f'http://127.0.0.1:{server.server_port}'
                with app.extensions['task_store'].connect() as db:
                    db.execute(instances.update().where(instances.c.id == identity).values(url=addresses[identity]))
            store = apps['manager'].extensions['task_store']
            worker = apps['worker'].extensions['task_store']
            # These fixtures deliberately do not start real task runners. Keep
            # their registry leases alive throughout the longer browser audit.
            def keep_online():
                while not heartbeat_stop.wait(2):
                    for fixture in apps.values():
                        fixture.extensions['fleet'].beat()
            heartbeat = threading.Thread(target=keep_online, daemon=True)
            heartbeat.start()
            for video in ['npNnbj46_ik', '2Gu7q-KUXCk', 'WMu6ANTg8mk']:
                store.create('download', {'urls': ['https://youtu.be/' + video]})
            store.activate_download_lists()
            worker.fleet.control('worker', {'action': 'resume'})
            old = worker.claim(worker.owner)
            errors = []
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True, **browser_options(pw.chromium))
                try:
                    page = browser.new_page(viewport={'width': 1440, 'height': 1050})
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    page.goto(addresses['manager'] + '/?instance_id=worker#settings')
                    form = page.locator('#settings-form')
                    expect(form.locator('[name=yt5s_quality]')).to_have_value('360')
                    form.locator('[name=yt5s_quality]').select_option('best')
                    form.locator('[name=downmp3_quality]').select_option('best')
                    form.locator('[name=downmp3_timeout]').fill('180')
                    form.locator('button[type=submit]').click()
                    expect(page.locator('#settings-save-state')).to_contain_text('后续领取')
                    expect(form.locator('button[type=submit]')).to_be_enabled()
                    new = worker.claim(worker.owner)
                    assert new['payload']['config']['yt5s_quality'] == 'best'
                    assert new['payload']['config']['downmp3_quality'] == 'best'
                    assert new['payload']['config']['downmp3_timeout'] == 180
                    assert worker.get(old['id'], private=True)['payload']['config']['yt5s_quality'] == '360'
                    assert store.fleet.policy()['config']['yt5s_quality'] == '360'
                    assert not store.downloads_paused()
                    page.screenshot(path=str(output / 'quality-hot-save.png'), full_page=True)
                    page.locator('[data-provider=downmp3] [data-method-move="-1"]').click()
                    form.locator('button[type=submit]').click()
                    expect(page.locator('#strategy-save-state')).to_contain_text('后续领取')
                    expect(page.locator('#toasts')).to_contain_text('设置已保存')
                    final = worker.claim(worker.owner)
                    assert final['payload']['config']['download_methods'] == ['downmp3', 'yt5s']
                    assert final['policy_version'] == worker.fleet.policy()['version']
                    assert worker.get(new['id'], private=True)['payload']['config']['download_methods'] == ['yt5s', 'downmp3']
                    page.screenshot(path=str(output / 'order-hot-save.png'), full_page=True)
                    form.locator('[name=scheduler_mode]').select_option('auto')
                    form.locator('[name=auto_channel_attempts]').fill('4')
                    form.locator('[name=shared_parser]').select_option('false')
                    form.locator('button[type=submit]').click()
                    expect(page.locator('#settings-save-state')).to_contain_text('后续领取')
                    expect(form.locator('button[type=submit]')).to_be_enabled()
                    page.reload()
                    expect(form.locator('[name=scheduler_mode]')).to_have_value('auto')
                    expect(form.locator('[name=auto_channel_attempts]')).to_have_value('4')
                    expect(form.locator('[name=shared_parser]')).to_have_value('false')
                    expect(form.locator('[name=throughput_mode]')).to_have_value('auto')
                    expect(form.locator('[name=throughput_mode]')).to_be_disabled()
                    assert store.fleet.local_settings()['config']['scheduler_mode'] == 'ordered_fallback'
                    # Restore ordered display for the instance-card checks below.
                    form.locator('[name=scheduler_mode]').select_option('ordered_fallback')
                    form.locator('button[type=submit]').click()
                    expect(page.locator('#settings-save-state')).to_contain_text('后续领取')
                    expect(form.locator('button[type=submit]')).to_be_enabled()
                    page.goto(addresses['manager'] + '/#instances')
                    card = page.locator('[data-instance-form=worker]')
                    expect(card).to_contain_text('下载顺序：downmp3 → yt5s')
                    card.get_by_role('link', name='下载策略与磁盘').click()
                    expect(page.locator('#fleet-selection')).to_have_value('worker')
                    expect(page.locator('[data-provider=downmp3]')).to_contain_text('优先使用')
                    page.set_viewport_size({'width': 390, 'height': 844})
                    assert page.evaluate('() => document.documentElement.scrollWidth <= window.innerWidth'), 'mobile overflow'
                    page.screenshot(path=str(output / 'order-mobile.png'), full_page=True)
                    page.set_viewport_size({'width': 1440, 'height': 1050})
                    check_settings_roundtrip(page, apps['worker'], root / 'worker/config.yaml')
                    assert not errors, errors
                finally:
                    browser.close()
            print('PASS: quality, timeout and provider-order saves apply on next claim; running snapshots preserved; no pause/restart.')
            print(output)
        finally:
            heartbeat_stop.set()
            if heartbeat:
                heartbeat.join(timeout=3)
            for server, thread in reversed(servers):
                server.shutdown()
                server.server_close()
                thread.join(timeout=3)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--hybrid', action='store_true')
    main(parser.parse_args().hybrid)
