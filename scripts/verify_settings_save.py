"""Exercise partial settings saves while a managed job is downloading."""
import argparse
from pathlib import Path
import re
import sys
import tempfile
import threading
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from playwright.sync_api import expect, sync_playwright
from werkzeug.serving import make_server

from app import create_app
from scripts.verify_jobs import QuietHandler
from webapp.browser import browser_options
from webapp.database import instances


def main(hybrid=False):
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        config = root / 'config.yaml'
        config.write_text('path: videos\nproxy_mode: direct\nmin_free_gb: 0\n'
                          'storage_mode: local\ndownload_methods: [downmp3]\n', encoding='utf-8')
        extra = {'role':'hybrid', 'instance_id':'local',
                 'database_url':'sqlite:///' + (root / 'shared.db').as_posix()} if hybrid else {}
        app = create_app(config, root / 'state', start_worker=False, **extra)
        store = app.extensions['task_store']
        if store.fleet:
            store.fleet.control(store.fleet.id, {'action':'resume'})
        client = app.test_client()
        csrf = re.search(r'name="csrf-token" content="([^"]+)"', client.get('/').text)[1]
        headers = {'X-CSRF-Token':csrf}
        created = client.post('/api/jobs', json={'customer':'Customer', 'name':'Running job',
            'urls':'npNnbj46_ik\n2Gu7q-KUXCk\nWMu6ANTg8mk'}, headers=headers)
        assert created.status_code == 201, created.json
        identity = created.json['id']
        second = client.post('/api/jobs', json={'customer':'Customer', 'name':'Next job',
            'urls':'aaaaaaaaaaa'}, headers=headers)
        assert second.status_code == 201, second.json
        with patch('app.require_downmp3_environment'), patch('webapp.fleet_routes.rpc'):
            response = client.post('/api/jobs/workbench',
                json={'action':'start','job_id':identity}, headers=headers)
        assert response.status_code == 200, response.json
        running = store.claim(store.owner)
        server = make_server('127.0.0.1', 0, app, threaded=True, request_handler=QuietHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        if store.fleet:
            store.fleet.url = f'http://127.0.0.1:{server.server_port}'
            with store.connect() as db:
                db.execute(instances.update().where(instances.c.id == store.fleet.id).values(url=store.fleet.url))
        errors, submitted = [], []
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True, **browser_options(pw.chromium))
                page = browser.new_page(viewport={'width':1440,'height':1050})
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.on('request', lambda req: submitted.append(req.post_data_json)
                        if req.method=='PUT' and req.url.endswith('/api/config') else None)
                page.goto(f'http://127.0.0.1:{server.server_port}/#settings')
                form = page.locator('#settings-form')
                expect(form.locator('[name=storage_mode]')).to_have_value('local')
                # Another page changes defaults after this form has loaded.
                response = client.put('/api/config', json={'storage_mode':'test'}, headers=headers)
                assert response.status_code == 200, response.json
                form.locator('[name=downmp3_quality]').select_option('720')
                with page.expect_response(lambda response: response.request.method=='PUT'
                                          and response.url.endswith('/api/config')) as saved:
                    form.locator('button[type=submit]').click()
                assert saved.value.status == 200, saved.value.text()
                expect(page.locator('#toasts')).to_contain_text('设置已保存')
                assert submitted[-1] == {'downmp3_quality':'720'}, submitted[-1]
                expect(form.locator('[name=storage_mode]')).to_have_value('test')
                assert client.get('/api/config').json['config']['storage_mode'] == 'test'
                assert store.get(running['id'], private=True)['payload']['config']['storage_mode'] == 'local'
                assert store.heartbeat(running['id'], store.owner)

                # Deliberately changing the default remains allowed during this job.
                form.locator('[name=storage_mode]').select_option('local')
                with page.expect_response(lambda response: response.request.method=='PUT'
                                          and response.url.endswith('/api/config')) as saved:
                    form.locator('button[type=submit]').click()
                assert saved.value.status == 200, saved.value.text()
                assert submitted[-1] == {'storage_mode':'local'}, submitted[-1]
                expect(form.locator('button[type=submit]')).to_be_enabled()
                page.locator('[data-provider=yt5s] [data-provider-toggle]').check()
                form.locator('button[type=submit]').click()
                expect(page.locator('#strategy-save-state')).not_to_contain_text('有未保存')
                assert submitted[-1] == {'download_methods':['downmp3','yt5s']}, submitted[-1]
                # An unchecked box with an unsaved draft must not start a job
                # using the older saved selection that still includes yt1d.
                expect(form.locator('button[type=submit]')).to_be_enabled()
                page.locator('[data-provider=yt1d] [data-provider-toggle]').check()
                form.locator('button[type=submit]').click()
                expect(page.locator('#strategy-save-state')).not_to_contain_text('有未保存')
                page.locator('[data-provider=yt1d] [data-provider-toggle]').uncheck()
                page.locator('[data-view=overview].nav-item').click()
                page.locator('#download-job').select_option(second.json['id'])
                expect(page.locator('#download-queue-start')).to_be_enabled()
                page.locator('#download-queue-start').click()
                expect(page.locator('#settings-view')).to_be_visible()
                expect(page.locator('#toasts')).to_contain_text('下载方式尚未保存完成')
                assert not client.get('/api/jobs/' + second.json['id']).json['released']
                form.locator('button[type=submit]').click()
                expect(page.locator('#strategy-save-state')).not_to_contain_text('有未保存')
                store.release(running['id'], store.owner, 'fixture next attempt', delay=0)
                next_task = store.claim('next-fixture-worker')
                assert next_task['progress']['selected_download_methods'] == ['downmp3','yt5s']
                assert next_task['progress']['download_method'] == 'downmp3'
                assert not store.downloads_paused()
                assert not errors, errors
                browser.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)
    print(f'PASS ({"hybrid" if hybrid else "standalone"}): changed fields only, no stale overwrite, instance mode saves during download, running attempt mode preserved.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--hybrid', action='store_true')
    main(parser.parse_args().hybrid)
