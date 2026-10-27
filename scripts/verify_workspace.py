"""Exercise page ownership, task-only imports, execution controls and navigation."""
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
from webapp.bulk import run_import
from webapp.jobs import Jobs


def main():
    output = Path('.cache/validation/workspace')
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        config = root / 'config.yaml'
        config.write_text('path: videos\nproxy_mode: direct\ndownload_methods: [downmp3]\n', encoding='utf-8')
        app = create_app(config, root / 'state', start_worker=False)
        store = app.extensions['task_store']
        jobs = Jobs(store)
        client = app.test_client()
        csrf = re.search(r'name="csrf-token" content="([^"]+)"', client.get('/').text)[1]
        headers = {'X-CSRF-Token': csrf}
        def create(name, urls):
            response = client.post('/api/jobs', json={'customer':'Customer', 'name':name, 'urls':urls}, headers=headers)
            assert response.status_code == 201, response.json
            return response.json['id']
        a = create('Order A', 'npNnbj46_ik\n2Gu7q-KUXCk')
        b = create('Order B', 'WMu6ANTg8mk')
        server = make_server('127.0.0.1', 0, app, threaded=True, request_handler=QuietHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f'http://127.0.0.1:{server.server_port}'
        errors, mutations = [], []
        try:
            with patch('app.require_downmp3_environment'), sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True, **browser_options(pw.chromium))
                page = browser.new_page(viewport={'width':1440,'height':1050})
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.on('request', lambda request: mutations.append(request.url) if request.method=='POST' else None)
                page.goto(base)
                expect(page.locator('#download-queue-start')).to_be_disabled()
                expect(page.locator('#overview-view textarea, #overview-view input[type=file]')).to_have_count(0)
                expect(page.locator('#overview-view [data-provider]')).to_have_count(0)
                home_nav = page.locator('nav[aria-label="主导航"] a').evaluate_all('(links)=>links.map(link=>[link.getAttribute("aria-label"),link.getAttribute("href")])')
                page.locator('#download-job').select_option(a)
                expect(page.locator('#download-job-summary')).to_contain_text('待下载2')
                expect(page.locator('#download-job-summary')).to_contain_text('当前实例 · 下载顺序：downmp3')
                page.locator('#download-queue-start').click()
                expect(page.locator('#download-queue-start')).to_have_text('任务已开始')
                first = store.claim(store.owner)
                assert first['batch_id'] == a
                expect(page.locator('#batch-progress')).to_contain_text('npNnbj46_ik')
                page.locator('#download-job-pause').click()
                expect(page.locator('#toasts')).to_contain_text('完成后暂停')
                assert not jobs.detail(a)['released']
                assert not jobs.detail(b)['released']
                assert store.heartbeat(first['id'], store.owner)
                store.finish(first['id'], worker_id=store.owner, result={})
                page.locator('#download-queue-start').click()
                expect(page.locator('#download-queue-start')).to_have_text('任务已开始')
                second = store.claim(store.owner)
                assert second['batch_id'] == a
                page.locator('#download-job-stop').click()
                expect(page.locator('#toasts')).to_contain_text('已停止当前实例')
                assert not store.heartbeat(second['id'], store.owner)
                assert not any(url.endswith('/videos') or url.endswith('/imports') for url in mutations)
                page.screenshot(path=str(output / 'download-desktop.png'), full_page=True)

                page.locator('#download-job-detail').click()
                expect(page.locator('#detail-heading h1')).to_have_text('Order A')
                task_nav = page.locator('nav[aria-label="主导航"] a').evaluate_all('(links)=>links.map(link=>[link.getAttribute("aria-label"),link.getAttribute("href")])')
                assert task_nav == home_nav, (task_nav,home_nav)
                expect(page.locator('[data-action=append]')).to_have_count(1)
                expect(page.locator('[data-action=start]')).to_have_count(0)
                page.locator('[data-action=append]').click()
                expect(page.locator('#basic-fields')).to_be_hidden()
                page.locator('#editor input[type=file]').set_input_files({'name':'more.txt','mimeType':'text/plain','buffer':b'WMu6ANTg8mk\nnpNnbj46_ik\n'})
                with page.expect_response(lambda response: response.url.endswith(f'/api/jobs/{a}/imports') and response.request.method=='POST') as uploaded:
                    page.locator('#editor button[type=submit]').click()
                task = store.get(uploaded.value.json()['id'], private=True)
                result = run_import(store, task['payload'], lambda value: None)
                assert result['added'] == 1 and result['duplicates'] == 1, result
                assert jobs.detail(a)['total'] == 3 and jobs.detail(b)['total'] == 1
                imported = store.claim(store.owner)
                assert imported['action'] == 'import'
                store.finish(imported['id'], worker_id=store.owner, result=result)
                expect(page.locator('#editor')).to_be_hidden()

                page.locator('[data-tab=settings]').click()
                expect(page.locator('[name=methods]')).to_have_count(0)
                expect(page.locator('#settings-form')).to_contain_text('下载方式、画质和保存模式由执行实例决定')
                page.locator('#settings-form [name=note]').fill('Task information only')
                page.locator('#settings-form button[type=submit]').click()
                expect(page.locator('#toast')).to_contain_text('已更新')
                assert jobs.detail(a)['policy_scope'] == 'instance'
                sidebar = page.locator('#sidebar').evaluate('(el)=>({top:el.getBoundingClientRect().top,position:getComputedStyle(el).position})')
                assert sidebar == {'top': 0, 'position': 'fixed'}, sidebar
                page.screenshot(path=str(output / 'task-settings-viewport.png'))
                page.screenshot(path=str(output / 'task-settings-desktop.png'), full_page=True)

                page.goto(base + '/#settings')
                expect(page.locator('#download-method')).to_be_visible()
                page.locator('[data-provider=yt5s] [data-provider-toggle]').check()
                page.locator('[data-provider=yt5s] [data-method-move="-1"]').click()
                page.locator('#settings-form button[type=submit]').click()
                expect(page.locator('#toasts')).to_contain_text('设置已保存')
                assert jobs.detail(a)['policy'] is None
                page.goto(base + '/?job=' + a + '#overview')
                expect(page.locator('#download-job-summary')).to_contain_text('当前实例 · 下载顺序：yt5s → downmp3')
                page.locator('#download-job-summary a').click()
                expect(page.locator('#settings-view')).to_be_visible()
                expect(page.locator('[data-provider=yt5s]')).to_contain_text('优先使用')
                page.goto(base + '/jobs#' + a)
                expect(page.locator('#detail-heading h1')).to_have_text('Order A')

                for view in ['overview','media','statistics','tasks','clash','settings']:
                    page.locator(f'.nav-item[href="/#'+view+'"]').click()
                    expect(page.locator(f'#{view}-view')).to_be_visible()
                    expect(page.locator('.nav-item.active')).to_have_attribute('href','/#'+view)
                    if view == 'statistics':
                        expect(page.locator('#billing-overview')).to_contain_text('预计费用合计')
                        expect(page.locator('.execution-audit')).not_to_have_attribute('open','')
                    if view == 'settings':
                        expect(page.locator('#download-method')).to_be_visible()
                        expect(page.locator('#statistics-pricing-form')).to_have_count(0)
                        expect(page.locator('#settings-form [name=url]')).to_have_count(0)
                    page.set_viewport_size({'width':390,'height':844})
                    assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth'), view
                    page.screenshot(path=str(output / f'{view}-mobile.png'), full_page=True)
                    page.set_viewport_size({'width':1440,'height':1050})
                page.goto(base + '/jobs#' + a)
                page.set_viewport_size({'width':390,'height':844})
                expect(page.locator('#detail-heading h1')).to_have_text('Order A')
                assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth'), 'task detail'
                page.locator('#jobs-mobile-menu').click()
                expect(page.locator('#sidebar')).to_have_class('sidebar mobile-open')
                page.screenshot(path=str(output / 'task-navigation-mobile.png'), full_page=True)
                assert not errors, errors
                browser.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)
    print('PASS: task-only video entry, selected execution controls, shared navigation, provider picker and all applicable pages on desktop/mobile.')
    print(output.resolve())


if __name__ == '__main__':
    main()
