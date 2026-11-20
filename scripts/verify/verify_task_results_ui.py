"""Isolated real-media task results, sample controls and responsive workspace."""
import json
from pathlib import Path
import sys
import threading
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tests.test_samples import SampleTests
from tests.test_console_auth import ConsoleAuthTests
from webapp.browser import browser_options
from werkzeug.serving import make_server, WSGIRequestHandler
from playwright.sync_api import sync_playwright, expect


class Quiet(WSGIRequestHandler):
    def log(self, *args):
        pass


fixture = SampleTests();fixture.setUp();server = None
output = Path('.runtime/verify/task-results');output.mkdir(parents=True, exist_ok=True)
try:
    fixture.storage = {'storage_mode': 'test'}
    job = fixture.create(['video_only', 'audio_only', 'av', 'subtitle', 'thumbnail', 'metadata'])
    task, _ = fixture.execute()
    fixture.token = lambda client: ConsoleAuthTests.token(fixture, client)
    ConsoleAuthTests.setup_admin(fixture)
    server = make_server('127.0.0.1', 0, fixture.app, threaded=True, request_handler=Quiet)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f'http://127.0.0.1:{server.server_port}'
    errors = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, **browser_options(pw.chromium))
        context = browser.new_context(viewport=dict(width=1440, height=1100))
        cookie = fixture.app.config['SESSION_COOKIE_NAME']
        context.add_cookies([dict(name=cookie, value=fixture.client.get_cookie(cookie).value, url=url)])
        page = context.new_page();page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto(url + '/jobs?view=videos')
        expect(page.get_by_role('heading', name='全部视频与文件')).to_be_visible()
        expect(page.get_by_text('测试样本可查看', exact=True)).to_be_visible()
        expect(page.get_by_role('link', name='素材库', exact=True)).to_have_count(0)
        page.get_by_role('button', name='查看结果 / 文件', exact=True).click()
        result = page.get_by_role('dialog', name='视频结果与文件', exact=True)
        expect(result).to_be_visible()
        expect(result.get_by_role('button', name='查看全文', exact=True)).to_have_count(2)
        expect(result.get_by_role('button', name='预览 / 下载', exact=True)).to_have_count(4)
        result.get_by_role('button', name='查看全文', exact=True).first.click()
        preview = page.get_by_role('dialog', name='fixture', exact=True)
        expect(preview.locator('pre')).to_contain_text('Hello world.')
        with page.expect_download() as download:
            preview.get_by_role('link', name='下载文件').click()
        assert download.value.suggested_filename
        preview.get_by_role('button', name='关闭对话框', exact=True).click()
        result.get_by_role('button', name='验收通过', exact=True).click()
        expect(result.get_by_text('人工验收通过', exact=False).first).to_be_visible()
        page.screenshot(path=str(output / 'results-desktop.png'), full_page=True)
        result.get_by_role('button', name='关闭对话框', exact=True).first.click()
        page.goto(url + f'/jobs/{job}?tab=testing')
        expect(page.get_by_role('heading', name='测试观察', exact=True)).to_be_visible()
        expect(page.get_by_text('100%', exact=True)).to_be_visible()
        page.locator('.sample-settings summary').click()
        page.get_by_label('每实例最多样本数').fill('2')
        page.get_by_role('button', name='保存留样设置', exact=True).click()
        expect(page.get_by_role('status').filter(has_text='设置已保存')).to_be_visible()
        page.screenshot(path=str(output / 'observation-desktop.png'), full_page=True)
        page.set_viewport_size(dict(width=390, height=844));page.reload()
        expect(page.get_by_role('heading', name='测试观察', exact=True)).to_be_visible()
        expect(page.get_by_text('100%', exact=True)).to_be_visible()
        page.screenshot(path=str(output / 'observation-mobile.png'), full_page=True)
        assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
        page.screenshot(path=str(output / 'observation-mobile.png'), full_page=True)
        page.get_by_role('button', name='核对内容 / 验收', exact=True).first.click()
        expect(page.get_by_role('dialog', name='视频结果与文件')).to_be_visible()
        assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
        page.screenshot(path=str(output / 'results-mobile.png'), full_page=True)
        assert not errors, errors
        browser.close()
    print(json.dumps(dict(desktop=True, mobile=True, real_subtitle_preview=True, download=True,
        review=True, policy_save=True, javascript_errors=errors)))
finally:
    if server:
        server.shutdown()
    fixture.doCleanups()
