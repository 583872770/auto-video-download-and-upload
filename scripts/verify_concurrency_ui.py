"""Exercise admission diagnostics in an isolated SQLite app and real Chromium."""
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from playwright.sync_api import expect, sync_playwright
from sqlalchemy import select
from werkzeug.serving import make_server, WSGIRequestHandler

from app import create_app
from webapp.database import download_permits, download_resources, instances, settings, tasks
from webapp.throughput import policy_key


class QuietHandler(WSGIRequestHandler):
    def log(self, *args, **kwargs):
        pass


def main():
    output = Path(__file__).resolve().parents[1] / '.cache/concurrency-preview'
    output.mkdir(parents=True, exist_ok=True)
    environment = {key: value for key, value in os.environ.items() if not key.startswith(('VIDEO_', 'MYSQL_', 'YT5S_'))}
    with tempfile.TemporaryDirectory(prefix='concurrency-ui-') as temporary, patch.dict(os.environ, environment, clear=True):
        root = Path(temporary)
        config = root / 'config.yaml'
        config.write_text('path: videos\nproxy_mode: direct\nworker_count: 16\nmin_free_gb: 0\n', encoding='utf-8')
        app = create_app(config, root / 'state', start_worker=False, role='hybrid', instance_id='preview',
                         instance_name='下载实例 A', database_url='sqlite:///' + (root / 'fixture.sqlite3').as_posix())
        store = app.extensions['task_store']
        fleet = store.fleet
        fleet.control('preview', {'action': 'resume'})
        cfg = fleet.local_settings()['config']
        for number, stage in enumerate(('downloading', 'downloading', 'extracting', 'waiting_transfer', 'waiting_transfer', 'waiting_transfer')):
            job = store.create('download', {'urls': ['https://youtu.be/jNQXAC9IVRw']})
            with store.connect() as db:
                db.execute(tasks.update().where(tasks.c.id == job['id']).values(status='running', instance_id='preview',
                    worker_id='fixture', lease_until=time.time()+600,
                    progress=json.dumps({'stage': stage, 'transfer_wait_resource': 'upstream' if stage == 'waiting_transfer' else None,
                                         'speed': 8*1024**2 if stage == 'downloading' else None})))
                if stage == 'downloading':
                    for scope, units in (('upstream:youtube-origin', 1), ('connections:youtube-origin', 2)):
                        db.execute(download_permits.insert().values(token=str(number),
                            resource=hashlib.sha256(scope.encode()).hexdigest(), task_id=job['id'], owner='fixture', units=units))

        def fixture(kind):
            stamp = time.time()
            rate = {'policy': policy_key(cfg), 'time': stamp, 'mode': 'manual', 'transfers': 16,
                    'workers': 16, 'browsers': 2, 'cpu_percent': 17.4, 'memory_available_mb': 5228,
                    'media_receive_mbps': 138.31, 'verified_mbps': 199.34, 'receive_mbps': 140,
                    'reason': '使用手动并发', 'groups': {}, 'channels': {}}
            fleet.beat(throughput=rate)
            with store.connect() as db:
                db.execute(download_resources.delete())
                db.execute(settings.delete().where(settings.c.key == 'disk:preview'))
                if kind in ('throttle', 'disk'):
                    value = {'cooldown_until': stamp + 95, 'recover_until': stamp + 155,
                             'cooldown_status': 429, 'cooldown_kind': 'subtitles'}
                    db.execute(download_resources.insert().values(
                        key=hashlib.sha256(b'upstream:youtube-origin').hexdigest(), state=json.dumps(value)))
                if kind == 'recovery':
                    db.execute(download_resources.insert().values(
                        key=hashlib.sha256(b'upstream:youtube-origin').hexdigest(),
                        state=json.dumps({'cooldown_until': stamp - 1, 'recover_until': stamp + 60})))
                if kind == 'subtitles':
                    db.execute(download_resources.insert().values(
                        key=hashlib.sha256(b'subtitles:youtube-origin').hexdigest(),
                        state=json.dumps({'cooldown_until': stamp + 120})))
                if kind == 'disk':
                    db.execute(store.db.upsert(settings, {'key': 'disk:preview', 'value': json.dumps({
                        'time': stamp, 'blocked': True, 'message': '测试下载分类预算不足，等待空间。'})}, ['key']))
                if kind == 'offline':
                    db.execute(instances.update().where(instances.c.id == 'preview').values(heartbeat=stamp-100))

        server = make_server('127.0.0.1', 0, app, threaded=True, request_handler=QuietHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        errors = []
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(executable_path='C:/Program Files/Google/Chrome/Application/chrome.exe', headless=True)
                page = browser.new_page(viewport={'width': 1440, 'height': 1150})
                page.on('pageerror', lambda error: errors.append(str(error)))
                panel = page.locator('[data-concurrency-panel=overview]')
                fixture('normal')
                page.goto(f'http://127.0.0.1:{server.server_port}/#overview')
                expect(panel).to_contain_text('单渠道上限 8 路')
                expect(panel.locator('.capacity-metrics>div').nth(1)).to_contain_text('8')
                panel.locator('summary').click()
                expect(panel.locator('.capacity-table')).to_be_visible()
                expect(panel).to_contain_text('解析与传输共用名额')
                page.screenshot(path=str(output/'normal-desktop.png'), full_page=True)
                fixture('throttle')
                expect(panel).to_contain_text('HTTP 429', timeout=12000)
                expect(panel).to_contain_text('字幕请求触发限流')
                expect(panel.locator('.capacity-metrics>div').nth(1)).to_contain_text('0')
                assert panel.locator('details').evaluate('node => node.open'), 'poll closed channel details'
                page.screenshot(path=str(output/'throttle-desktop.png'), full_page=True)
                page.set_viewport_size({'width': 390, 'height': 844})
                page.locator('#sidebar').evaluate('node => Promise.all(node.getAnimations().map(animation => animation.finished))')
                page.screenshot(path=str(output/'throttle-mobile.png'), full_page=True)
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), 'mobile horizontal overflow'
                fixture('recovery')
                expect(panel).to_contain_text('相关共享资源暂按 1 路放行', timeout=12000)
                expect(panel.locator('.capacity-metrics>div').nth(1)).to_contain_text('1')
                fixture('disk')
                expect(panel).to_contain_text('临时空间等待', timeout=12000)
                expect(panel).to_contain_text('HTTP 429')
                fixture('offline')
                expect(panel).to_contain_text('实例离线', timeout=12000)
                expect(panel.locator('.capacity-metrics>div').nth(1).locator('strong')).to_contain_text('—')
                fixture('throttle')
                page.set_viewport_size({'width': 1440, 'height': 1150})
                page.goto(f'http://127.0.0.1:{server.server_port}/#settings')
                settings_panel = page.locator('[data-concurrency-panel=settings]')
                expect(settings_panel).to_contain_text('HTTP 429', timeout=12000)
                settings_panel.scroll_into_view_if_needed()
                page.screenshot(path=str(output/'settings-desktop.png'))
                draft = page.locator('#concurrency-config-note')
                expect(draft).to_have_class('micro-copy capacity-draft-warning')
                expect(draft).to_contain_text('最多约 8 路')
                page.locator('[name=builtin_transfer_limit]').fill('16')
                page.locator('[name=builtin_connection_limit]').fill('32')
                page.locator('[name=subtitle_mode]').select_option('optional')
                page.locator('[name=subtitle_languages]').fill('en,zh-Hans')
                expect(draft).not_to_have_class('micro-copy capacity-draft-warning')
                page.locator('#settings-form button[type=submit]').click()
                expect(page.locator('#settings-save-state')).to_contain_text('设置已保存', timeout=12000)
                page.reload()
                expect(page.locator('[name=builtin_transfer_limit]')).to_have_value('16')
                expect(page.locator('[name=builtin_connection_limit]')).to_have_value('32')
                expect(page.locator('[name=subtitle_languages]')).to_have_value('en,zh-Hans')
                child = store.create('subtitles', {'base': str(root), 'config': cfg})
                fixture('subtitles')
                expect(settings_panel).to_contain_text('字幕独立补下载', timeout=12000)
                expect(settings_panel).to_contain_text('字幕上游冷却')
                expect(settings_panel).not_to_contain_text('渠道冷却中')
                expect(settings_panel.locator('.capacity-metrics>div').nth(1)).to_contain_text('16')
                page.screenshot(path=str(output/'settings-optimized.png'), full_page=True)
                page.route('**/api/instances', lambda route: route.abort())
                expect(settings_panel).to_contain_text('状态暂未更新', timeout=26000)
                expect(settings_panel.locator('.capacity-metrics>div').nth(1).locator('strong')).to_contain_text('—')
                assert not errors, errors
                browser.close()
        finally:
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()
            store.db.engine.dispose()
        print(json.dumps({'scenarios': ['normal', '429', 'recovery', 'disk', 'offline', 'settings', 'save-reload', 'subtitle-cooldown', 'stale-request'],
                          'mobile_overflow': False, 'page_errors': errors, 'output': str(output)}, ensure_ascii=False))


if __name__ == '__main__':
    main()
