"""Vue routing, uncertain writes, imports, file paging and safe previews on isolated data."""
import json
from pathlib import Path
import sys
import threading
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from playwright.sync_api import Error as BrowserError, expect, sync_playwright
from werkzeug.serving import make_server, WSGIRequestHandler
from tests.test_console_auth import ConsoleAuthTests
from webapp.browser import browser_options
from webapp.database import delivery_artifacts


class QuietHandler(WSGIRequestHandler):
    def log_request(self, *args, **kwargs):
        pass


def main():
    fixture = ConsoleAuthTests()
    fixture.setUp()
    server = None
    try:
        fixture.setup_admin()
        assert 'frontend-app' in fixture.client.get('/jobs').text, 'Build frontend first'
        server = make_server('127.0.0.1', 0, fixture.app, threaded=True, request_handler=QuietHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f'http://127.0.0.1:{server.server_port}'
        output = Path('.runtime/verify/frontend-workspace')
        output.mkdir(parents=True, exist_ok=True)
        errors, writes = [], []
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, **browser_options(pw.chromium))
            context = browser.new_context(viewport={'width':1440, 'height':1050}, reduced_motion='reduce')
            name = fixture.app.config['SESSION_COOKIE_NAME']
            context.add_cookies([dict(name=name, value=fixture.client.get_cookie(name).value, url=base)])
            page = context.new_page()
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.on('request', lambda request: writes.append(request) if request.method in ('POST','PUT') and '/api/jobs' in request.url else None)
            page.goto(base + '/jobs')
            assert not page.locator('script[src$="/jobs.js"]').count()
            expect(page.locator('#task-table')).to_be_visible()
            expect(page.get_by_role('button',name='打开导航',exact=True)).to_be_hidden()
            page.screenshot(path=str(output / 'jobs-empty-1440.png'), full_page=True)

            # A nested discard prompt must remain clickable inside a native modal.
            page.locator('#new-task').click()
            page.locator('#editor [name=name]').fill('Unsaved modal draft')
            page.get_by_role('button',name='关闭对话框',exact=True).click()
            page.get_by_role('button',name='继续编辑',exact=True).click()
            expect(page.locator('#editor [name=name]')).to_have_value('Unsaved modal draft')
            page.get_by_role('button',name='关闭对话框',exact=True).click()
            page.get_by_role('button',name='放弃并继续',exact=True).click()
            expect(page.locator('#editor')).to_have_count(0)

            # Simulate a committed create whose HTTP result was lost. Retry must reuse its UUID.
            create_ids = []
            def create(route):
                if route.request.method != 'POST':
                    route.continue_(); return
                create_ids.append(route.request.post_data_json['request_id'])
                response = route.fetch()
                assert response.status == 201
                if len(create_ids) == 1:
                    route.fulfill(status=503, json={'error':'fixture response lost'})
                else:
                    route.fulfill(response=response)
            page.route('**/api/jobs', create)
            page.locator('#new-task').click()
            page.locator('#editor [name=customer]').fill('Vue customer')
            page.locator('#editor [name=name]').fill('Vue order')
            page.locator('#editor [name=price]').fill('0.000001')
            page.locator('#editor [name=unit]').select_option('GiB')
            page.locator('#editor [type=submit]').click()
            expect(page.locator('#editor-error')).to_contain_text('fixture response lost')
            expect(page.locator('#editor [name=name]')).to_have_value('Vue order')
            page.locator('#editor [type=submit]').click()
            expect(page.locator('#detail-heading h1')).to_have_text('Vue order')
            identity = urlsplit(page.url).path.rsplit('/',1)[-1]
            assert len(create_ids) == 2 and create_ids[0] == create_ids[1]
            assert len(fixture.client.get('/api/jobs').json['items']) == 1
            detail = fixture.client.get('/api/jobs/' + identity).json
            assert detail['unit'] == 'GiB' and detail['price'] == '0.000001'
            assert detail['delivery']['contract']['spec']['outputs'] == ['av']
            page.unroute('**/api/jobs')

            # A slow old search cannot repaint a newer empty result.
            original = fixture.client.get('/api/jobs').json
            page.get_by_role('link', name='任务中心', exact=True).click()
            expect(page.locator('#task-table')).to_contain_text('Vue order')
            page.screenshot(path=str(output / 'jobs-1440.png'), full_page=True)
            held = []
            def slow_search(route):
                if 'q=slow' in route.request.url: held.append(route)
                else: route.continue_()
            page.route('**/api/jobs?**', slow_search)
            with page.expect_request(lambda request: '/api/jobs?' in request.url and 'q=slow' in request.url):
                page.locator('#search').fill('slow')
            page.locator('#search').fill('definitely-absent')
            expect(page.locator('#task-table')).to_contain_text('暂无符合条件的任务')
            for delayed in held:
                try: delayed.fulfill(json=original)
                except BrowserError: pass  # The superseded fetch has already been aborted.
            expect(page.locator('#task-table')).not_to_contain_text('Vue order')
            page.unroute('**/api/jobs?**')
            page.goto(base + '/jobs/' + identity + '?tab=videos')
            expect(page.locator('#detail-heading h1')).to_have_text('Vue order')

            # A successful paste followed by a failed file upload must not repeat the paste.
            append_requests = []
            page.on('request', lambda request: append_requests.append(request) if request.method == 'POST' and request.url.endswith('/videos') else None)
            uploads = []
            def upload(route):
                uploads.append(route.request)
                if len(uploads) == 1: route.fulfill(status=503,json={'error':'fixture upload unavailable'})
                else: route.continue_()
            page.route('**/imports', upload)
            page.locator('[data-action=append]').click()
            page.locator('#editor textarea').fill('npNnbj46_ik')
            page.locator('#editor [type=file]').set_input_files({'name':'more.txt','mimeType':'text/plain','buffer':b'2Gu7q-KUXCk\n'})
            page.locator('#editor [type=submit]').click()
            expect(page.locator('#editor [role=alert]')).to_contain_text('fixture upload unavailable')
            expect(page.locator('#editor textarea')).to_have_value('')
            page.locator('#editor [type=submit]').click()
            expect(page.locator('#editor')).to_have_count(0)
            expect(page.locator('#detail-content tbody')).to_contain_text('npNnbj46_ik')
            assert len(append_requests) == 1 and len(uploads) == 2
            assert all('x-instance-id' not in request.headers for request in writes)
            page.unroute('**/imports')

            # Route changes preserve a dirty task draft until the user explicitly discards it.
            page.locator('[data-tab=settings]').click()
            page.locator('#settings-form [name=name]').fill('Unsaved order')
            nav = page.get_by_role('navigation',name='主导航')
            nav.get_by_role('link',name='系统设置',exact=True).click()
            page.get_by_role('button',name='继续编辑',exact=True).click()
            expect(page.locator('#settings-form [name=name]')).to_have_value('Unsaved order')
            nav.get_by_role('link',name='系统设置',exact=True).click()
            page.get_by_role('button',name='放弃并继续',exact=True).click()
            page.wait_for_selector('[data-ready=true]')
            page.evaluate('window.__workspaceToken = 42')
            nav.get_by_role('link',name='素材库',exact=True).click()
            expect(page.get_by_role('heading',name='素材库',exact=True)).to_be_visible()
            assert page.evaluate('window.__workspaceToken') == 42, 'Navigation reloaded the document'

            # Real local subtitle objects: cursor paging, job scope, plain-text preview and Range.
            subtitle = fixture.root / 'caption.srt'
            subtitle.write_text('1\n00:00:00,000 --> 00:00:01,000\n<script>window.__unsafeSubtitle = true</script>\n',encoding='utf-8')
            with fixture.store.connect() as db:
                for index in range(51):
                    db.execute(delivery_artifacts.insert().values(id=f'{index:064x}',seq=index+1,job_id=identity,task_id='f'*32,video_id='npNnbj46_ik',title=f'字幕 {index:02d}',kind='subtitle',language='en',variant='0'*64,path=str(subtitle),size=subtitle.stat().st_size,instance_id='local',storage='local',object_key='',available=1,test_mode=0,created_at='2026-11-13T00:00:00'))
            page.goto(base + '/media')
            expect(page.locator('#asset-table tbody tr')).to_have_count(50)
            page.locator('#media-next').click()
            expect(page.locator('#asset-table tbody tr')).to_have_count(1)
            page.locator('#media-prev').click()
            expect(page.locator('#asset-table tbody tr')).to_have_count(50)
            page.locator('[data-preview]').first.click()
            expect(page.locator('dialog pre')).to_contain_text('<script>window.__unsafeSubtitle')
            assert page.evaluate('window.__unsafeSubtitle') is None
            page.get_by_role('button',name='关闭对话框',exact=True).click()
            content = page.request.get(base + '/api/delivery/files/' + f'{50:064x}',headers={'Range':'bytes=0-9'})
            assert content.status == 206 and len(content.body()) == 10
            page.get_by_role('textbox',name='搜索素材',exact=True).fill('absent')
            page.get_by_role('button',name='查询',exact=True).click()
            expect(page.locator('#asset-table')).to_contain_text('暂无符合条件的资产')
            expect(page.locator('#media-next')).to_be_disabled()
            page.get_by_role('textbox',name='搜索素材',exact=True).fill('')
            page.get_by_role('button',name='查询',exact=True).click()
            expect(page.locator('#asset-table tbody tr')).to_have_count(50)
            for width in (1440,768,390):
                page.set_viewport_size({'width':width,'height':1000})
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), f'overflow {width}'
                page.screenshot(path=str(output/f'media-{width}.png'),full_page=True,animations='disabled')
                if width == 390:
                    assert page.locator('.sidebar').bounding_box()['x'] <= -220
                    page.get_by_role('button',name='打开导航',exact=True).click()
                    expect(page.locator('.sidebar')).to_have_class('sidebar open')
                    page.get_by_role('button',name='关闭导航',exact=True).click()
                    expect(page.locator('.sidebar')).to_have_class('sidebar')
            assert not errors, errors
            browser.close()
        print(json.dumps({'ok':True,'browser_errors':errors,'screenshots':str(output)},ensure_ascii=False))
    finally:
        if server:
            server.shutdown(); server.server_close()
        fixture.doCleanups()


if __name__ == '__main__':
    main()
