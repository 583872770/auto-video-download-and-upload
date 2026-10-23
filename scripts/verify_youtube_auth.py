"""Exercise login preflight UI with synthetic cookies and network results."""
import json
from pathlib import Path
import sys
import threading
from unittest.mock import patch
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from playwright.sync_api import sync_playwright, expect
from werkzeug.serving import make_server

from app import create_app
from scripts.verify_web import QuietHandler
from Uploaders.browser import browser_options
from Utils.runtime import ROOT


def main():
    output = ROOT / '.cache/validation' / ('youtube-auth-' + uuid.uuid4().hex[:10])
    output.mkdir(parents=True)
    config = output / 'config.yaml'
    config.write_text('path: videos\nproxy_mode: direct\n', encoding='utf-8')
    app = create_app(config, output / 'state', start_worker=False)
    store = app.extensions['task_store']
    task = store.create('download', {'config': {}, 'base': str(output), 'urls': ['https://youtu.be/npNnbj46_ik']})
    result = {'value': ('not_logged_in', '测试 Cookie 尚未登录，请重新登录 YouTube。')}
    errors = []
    server = make_server('127.0.0.1', 0, app, threaded=True, request_handler=QuietHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with patch('webapp.youtube_auth.probe', side_effect=lambda *args: result['value']), sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, **browser_options(pw.chromium))
            try:
                page = browser.new_page(viewport={'width': 1440, 'height': 1080}, reduced_motion='reduce')
                page.on('pageerror', lambda error: errors.append(str(error)))
                base = f'http://127.0.0.1:{server.server_port}'
                page.goto(base, wait_until='networkidle')
                page.locator('#download-urls').fill('2Gu7q-KUXCk')
                page.locator('#download-form button[type=submit]').click()
                expect(page.locator('#youtube-auth-status')).to_contain_text('请先登录 YouTube')
                expect(page.locator('#download-queue-toggle')).to_have_text('继续下载')
                assert store.counts()['total'] == 1
                assert store.get(task['id'])['attempts'] == 0
                expect(page.locator('#all-tasks .task-row')).to_have_count(0)

                page.locator('.youtube-cookie-row [data-open=youtube-cookie-dialog]').click()
                cookie = b'# HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tSID\tlocal-fixture-only\n'
                page.locator('#youtube-cookie-form input[type=file]').set_input_files(
                    {'name': 'cookies.txt', 'mimeType': 'text/plain', 'buffer': cookie})
                page.locator('#youtube-cookie-form button[type=submit]').click()
                expect(page.locator('#youtube-cookie-dialog')).not_to_be_visible()
                page.locator('#youtube-auth-check').click()
                expect(page.locator('#youtube-auth-status')).to_contain_text('测试 Cookie 尚未登录')
                expect(page.locator('#download-queue-status')).to_contain_text('队列已停止')

                result['value'] = ('network_error', '测试代理连接失败，请检查节点。')
                page.locator('#youtube-auth-check').click()
                expect(page.locator('#youtube-auth-status')).to_contain_text('测试代理连接失败')
                page.screenshot(path=str(output / 'blocked-desktop.png'), full_page=True)
                page.set_viewport_size({'width': 390, 'height': 844})
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                page.screenshot(path=str(output / 'blocked-mobile.png'), full_page=True)

                result['value'] = ('valid', 'YouTube 登录验证通过。')
                page.locator('#youtube-auth-check').click()
                expect(page.locator('#youtube-auth-status')).to_contain_text('登录验证通过')
                expect(page.locator('.youtube-cookie-row [data-youtube-status]')).to_have_text('YouTube 登录已验证')
                assert store.downloads_paused()
                page.locator('#download-queue-toggle').click()
                expect(page.locator('#download-queue-toggle')).to_have_text('停止全部下载')
                assert not store.downloads_paused()
                page.locator('#download-form button[type=submit]').click()
                expect(page.locator('#stat-active')).to_have_text('2')
                assert store.counts()['total'] == 2
                assert not store.counts().get('failed')
                expect(page.locator('#batch-progress .batch-video')).to_have_count(0)
                assert not errors, errors
            finally:
                browser.close()
    finally:
        server.shutdown()
        server.server_close()
    report = {'synthetic_network_only': True, 'missing_cookie_blocks_submit': 'passed',
              'invalid_cookie_and_network_error_distinguished': 'passed', 'queue_preserved': 'passed',
              'verified_resume_and_submit': 'passed', 'mobile_layout': 'passed', 'browser_errors': errors}
    (output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print('YOUTUBE_AUTH_UI_VERIFIED:', output / 'report.json')


if __name__ == '__main__':
    main()
