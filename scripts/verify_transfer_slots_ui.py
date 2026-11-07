"""Verify real permit ownership and the twelve-slot workbench in isolated SQLite."""
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
from werkzeug.serving import make_server, WSGIRequestHandler

from app import create_app
from webapp.database import download_permits, download_resources, instances, tasks


class QuietHandler(WSGIRequestHandler):
    def log(self, *args, **kwargs):
        pass


def main():
    output = Path(__file__).resolve().parents[1] / '.cache/transfer-slots-preview'
    output.mkdir(parents=True, exist_ok=True)
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith(('VIDEO_', 'MYSQL_', 'YT5S_'))}
    with tempfile.TemporaryDirectory(prefix='transfer-slots-') as temporary, patch.dict(os.environ, environment, clear=True):
        root = Path(temporary)
        config = root / 'config.yaml'
        config.write_text('path: videos\nproxy_mode: direct\nworker_count: 16\nmin_free_gb: 0\n'
                          'builtin_transfer_limit: 12\nbuiltin_connection_limit: 64\n'
                          'download_connection_limit: 64\n', encoding='utf-8')
        app = create_app(config, root / 'state', start_worker=False, role='hybrid',
                         instance_id='preview', instance_name='预览实例',
                         database_url='sqlite:///' + (root / 'fixture.sqlite3').as_posix())
        store = app.extensions['task_store']
        store.fleet.control('preview', {'action': 'resume'})
        stages = ['downloading'] * 4 + ['waiting_request'] * 2 + ['extracting',
                  'extracting', 'waiting_transfer', 'processing']
        held_ids = []
        for index, stage in enumerate(stages):
            job = store.create('download', {'urls': ['https://youtu.be/jNQXAC9IVRw']})
            if index < 7:
                held_ids.append(job['id'])
            with store.connect() as db:
                db.execute(tasks.update().where(tasks.c.id == job['id']).values(
                    status='running', instance_id='preview', worker_id='fixture', lease_until=time.time() + 600,
                    progress=json.dumps({'stage': stage, 'title': f'示例视频 {index + 1:02d}',
                                         'cookie_profile_name': f'account{index + 1:02d}@example.test',
                                         'speed': 8 * 1024**2 if stage == 'downloading' else None})))
                if index < 7:
                    for scope in ('local-transfers:preview', 'upstream:youtube-origin'):
                        db.execute(download_permits.insert().values(token=str(index),
                            resource=hashlib.sha256(scope.encode()).hexdigest(),
                            task_id=job['id'], owner='fixture', units=1))
        store.fleet.beat()
        server = make_server('127.0.0.1', 0, app, threaded=True, request_handler=QuietHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        errors = []
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True)
                page = browser.new_page(viewport={'width': 1440, 'height': 1100}, device_scale_factor=1)
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.goto(f'http://127.0.0.1:{server.server_port}/#overview')
                panel = page.locator('[data-concurrency-panel=overview]')
                slots = panel.locator('.transfer-slot')
                expect(slots).to_have_count(12)
                expect(panel.locator('.transfer-slot.transferring')).to_have_count(4)
                expect(panel.locator('.transfer-slot.waiting')).to_have_count(2)
                expect(panel.locator('.transfer-slot.preparing')).to_have_count(1)
                expect(panel.locator('.transfer-slot.unoccupied')).to_have_count(5)
                expect(panel.locator('.transfer-outside')).to_contain_text('另有 3 个视频未占用传输名额')
                expect(panel.locator('.transfer-outside')).to_contain_text('合并 / 后处理 1')
                assert page.locator('.capacity-metrics').count() == 0
                expect(page.locator('#speed-caption')).not_to_contain_text('传输中视频')
                assert panel.locator('.capacity-explanations').evaluate('node => !node.open')
                panel.screenshot(path=str(output / 'twelve-desktop.png'))
                slots.first.focus()
                focused_id = slots.first.get_attribute('data-task')
                page.evaluate('renderConcurrency()')
                assert page.evaluate('document.activeElement.dataset.task') == focused_id
                slots.first.click()
                expect(page.locator('#task-dialog')).to_be_visible()
                page.locator('#task-dialog .dialog-heading [data-close]').click()
                page.set_viewport_size({'width': 390, 'height': 844})
                page.locator('#sidebar').evaluate('node => Promise.all(node.getAnimations().map(animation => animation.finished))')
                panel.screenshot(path=str(output / 'twelve-mobile.png'))
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), 'mobile horizontal overflow'
                # A shared restriction leaves holders running, reducing only the remaining allowance.
                with store.connect() as db:
                    db.execute(download_resources.insert().values(
                        key=hashlib.sha256(b'upstream:youtube-origin').hexdigest(),
                        state=json.dumps({'policies': {'fixture': {'time': time.time(), 'limit': 8}}})))
                expect(panel.locator('.transfer-slot.restricted')).to_have_count(4, timeout=12000)
                expect(panel.locator('.transfer-slot.unoccupied')).to_have_count(1)
                expect(panel.locator('.transfer-slot.transferring')).to_have_count(4)
                # The configured ceiling is retained during offline/stale states, without invented idle slots.
                with store.connect() as db:
                    db.execute(instances.update().where(instances.c.id == 'preview').values(heartbeat=time.time() - 100))
                expect(panel.locator('.transfer-slot.unknown')).to_have_count(12, timeout=12000)
                expect(panel.locator('.transfer-slot.unoccupied')).to_have_count(0)
                expect(panel.locator('.transfer-outside')).to_have_count(0)
                expect(panel.locator('.transfer-effective b')).to_have_text('—')
                store.fleet.beat()
                expect(panel.locator('.transfer-slot.transferring')).to_have_count(4, timeout=12000)
                page.route('**/api/instances', lambda route: route.abort())
                expect(panel.locator('.transfer-slot.unknown')).to_have_count(12, timeout=26000)
                # Untrusted titles and Cookie names remain plain text, including in tooltips.
                html = page.evaluate("""() => transferSlotsHTML({online:true, time:Date.now()/1000,
                    transfer_allocation:{configured:1,effective:1,unoccupied:0,restricted:0,outside:{},
                    occupied:[{task_id:'test',stage:'downloading',title:'<img src=x onerror=alert(1)>',cookie_name:'<script>bad</script>'}]}})""")
                assert '<img ' not in html and '<script>' not in html
                assert '&lt;img ' in html and '&lt;script&gt;' in html
                assert not errors, errors
                browser.close()
        finally:
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()
            store.db.engine.dispose()
        print(json.dumps({'slots': 12, 'scenarios': ['permit-stage', 'outside-stages', 'task-dialog',
            'shared-restriction', 'offline', 'stale', 'escaped-text', 'mobile'],
            'page_errors': errors, 'output': str(output)}, ensure_ascii=False))


if __name__ == '__main__':
    main()
