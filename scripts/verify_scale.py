"""Browser verification of bulk import, two real download processes and pagination."""
import json
from pathlib import Path
import sys
import threading
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from flask import Response
from playwright.sync_api import expect, sync_playwright
import psycopg
from psycopg import sql
from werkzeug.serving import make_server

from app import create_app
from scripts.verify_web import QuietHandler
from Uploaders.browser import browser_options
from Utils.runtime import ROOT


def main():
    output = ROOT / '.cache/validation' / ('scale-ui-' + uuid.uuid4().hex[:10])
    state = output / 'state'
    state.mkdir(parents=True)
    c = json.loads((ROOT / '.cache/postgres/connection.json').read_text(encoding='utf-8'))
    name = 'ui_video_' + uuid.uuid4().hex[:12]
    admin = psycopg.connect(host='127.0.0.1', port=c['port'], user=c['user'], password=c['password'],
                           dbname='postgres', autocommit=True)
    admin.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
    url = f'postgresql+psycopg://{c["user"]}:{c["password"]}@127.0.0.1:{c["port"]}/{name}'
    (state / 'database.json').write_text(json.dumps({'url': url}), encoding='utf-8')
    config = output / 'config.yaml'
    config.write_text('path: videos\nproxy_mode: direct\n', encoding='utf-8')
    app = create_app(config, state)
    body = b'bulk transport fixture\n' * 32768

    @app.route('/fixture/<name>.mp4', methods=['GET', 'HEAD'])
    def fixture(name):
        def chunks():
            for offset in range(0, len(body), 65536):
                time.sleep(0.01)
                yield body[offset:offset + 65536]
        return Response(chunks(), mimetype='video/mp4', headers={'Content-Length': str(len(body))})

    server = make_server('127.0.0.1', 0, app, threaded=True, request_handler=QuietHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    errors, report = [], {'database': 'PostgreSQL', 'actual_local_downloads': 64, 'youtube_downloads': 0}
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, **browser_options(pw.chromium))
            try:
                page = browser.new_page(viewport={'width': 1440, 'height': 1080}, reduced_motion='reduce')
                page.on('pageerror', lambda error: errors.append(str(error)))
                base = f'http://127.0.0.1:{server.server_port}'
                page.goto(base, wait_until='networkidle')
                page.locator('.nav-item[data-view=settings]').click()
                page.locator('[name=worker_count]').fill('2')
                page.locator('[name=min_free_gb]').fill('0')
                page.locator('#settings-form button[type=submit]').click()
                expect(page.locator('.toast').last).to_contain_text('保存')
                assert page.request.get(base + '/api/status').json()['worker_count'] == 2
                page.locator('.nav-item[data-view=overview]').click()
                page.locator('[data-open=bulk-import-dialog]').click()
                source = 'title,url\n' + ''.join(f'测试 {i},{base}/fixture/source{i:03d}.mp4\n' for i in range(64))
                source += f'重复,{base}/fixture/source000.mp4\n无效,bad-id\n'
                page.locator('#bulk-import-form input[type=file]').set_input_files(
                    {'name': '下载清单.csv', 'mimeType': 'text/csv', 'buffer': source.encode('utf-8')})
                with page.expect_response(lambda response: response.request.method == 'POST' and response.url.endswith('/api/downloads/import')) as uploaded:
                    page.locator('#bulk-import-form button[type=submit]').click()
                import_id = uploaded.value.json()['id']
                expect(page.locator('#task-dialog')).to_be_visible()
                expect(page.locator('#task-result')).to_contain_text('新增 64，重复 1，无效 1', timeout=45000)
                import_task = page.request.get(base + '/api/tasks/' + import_id).json()
                batch = import_task['result']['batch_id']
                report['import_counts'] = import_task['result']
                page.locator('#task-dialog [data-close]').click()
                expect(page.locator('#batch-progress .batch-video')).to_have_count(2, timeout=15000)
                expect(page.locator('#batch-count')).to_have_text('2')
                deadline = time.monotonic() + 150
                while time.monotonic() < deadline:
                    counts = page.request.get(base + '/api/overview').json()['counts']
                    if not counts.get('running', 0) and not counts.get('queued', 0):
                        break
                    page.wait_for_timeout(500)
                else:
                    raise AssertionError('Local downloads did not finish before timeout')
                tasks = page.request.get(base + '/api/tasks?batch_id=' + batch + '&limit=100').json()['items']
                assert len(tasks) == 64 and all(task['status'] == 'succeeded' for task in tasks)
                assert {task['progress']['worker_slot'] for task in tasks} == {0, 1}
                assert len(list((output / 'videos').rglob('*.mp4'))) == 64
                report['download_processes'] = sorted({task['progress']['worker_slot'] for task in tasks})
                page.locator('.nav-item[data-view=tasks]').click()
                expect(page.locator('#all-tasks .task-row')).to_have_count(50, timeout=15000)
                page.locator('[data-page=tasks][data-direction=next]').click()
                expect(page.locator('#all-tasks .task-row')).to_have_count(15)
                target = tasks[0]
                page.locator('#task-search').fill(target['id'])
                expect(page.locator('#all-tasks .task-row')).to_have_count(1)
                expect(page.locator('#all-tasks .task-row')).to_have_attribute('data-task', target['id'])
                page.locator('.nav-item[data-view=media]').click()
                expect(page.locator('#all-media .media-card')).to_have_count(24)
                page.locator('[data-page=media][data-direction=next]').click()
                expect(page.locator('#media-page-label')).to_contain_text('2')
                page.locator('#media-search').fill('source000')
                expect(page.locator('#all-media .media-card')).to_have_count(1)
                page.locator('#media-search').fill('')
                expect(page.locator('#all-media .media-card')).to_have_count(24)
                page.screenshot(path=str(output / 'library-desktop.png'), full_page=True)
                page.set_viewport_size({'width': 390, 'height': 844})
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                page.screenshot(path=str(output / 'library-mobile.png'), full_page=True)
                assert not errors, errors
                report.update(pagination_and_search='passed', settings='passed', browser_errors=errors)
            finally:
                browser.close()
    finally:
        runner = app.extensions['task_runner']
        runner.stop.set()
        for child in runner.processes.values():
            child.wait(timeout=60)
        server.shutdown()
        server.server_close()
        admin.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(name)))
        admin.close()
    (output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print('SCALE_UI_VERIFIED:', output / 'report.json')


if __name__ == '__main__':
    main()
