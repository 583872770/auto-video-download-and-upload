"""Verify recorded authentication failures in an isolated app and real Chromium."""
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from playwright.sync_api import expect, sync_playwright
from werkzeug.serving import make_server, WSGIRequestHandler

from app import create_app
from webapp.database import settings


class QuietHandler(WSGIRequestHandler):
    def log(self, *args, **kwargs):
        pass


def main():
    output = Path(__file__).resolve().parents[2] / '.runtime/cache-archive-20261111/verification-notice-preview'
    output.mkdir(parents=True, exist_ok=True)
    environment = {k: v for k, v in os.environ.items() if not k.startswith(('VIDEO_', 'MYSQL_', 'YT5S_'))}
    with tempfile.TemporaryDirectory(prefix='verification-ui-', dir=output.parent) as temporary, \
            patch.dict(os.environ, environment, clear=True):
        root = Path(temporary)
        config = root / 'config.yaml'
        config.write_text('path: videos\nproxy_mode: direct\ndownload_method: yt_dlp\nmin_free_gb: 0\n', encoding='utf-8')
        app = create_app(config, root / 'state', start_worker=False, role='hybrid', instance_id='preview',
                         database_url='sqlite:///' + (root / 'fixture.sqlite3').as_posix())
        store = app.extensions['task_store']
        store.fleet.beat()
        auth_key = 'i:' + hashlib.sha256(b'preview').hexdigest()[:16] + ':youtube_auth_latest'

        def save(auth):
            with store.connect() as db:
                db.execute(store.db.upsert(settings, {'key': auth_key, 'value': json.dumps(auth)}, ['key']))

        server = make_server('127.0.0.1', 0, app, threaded=True, request_handler=QuietHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        errors = []
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(executable_path='C:/Program Files/Google/Chrome/Application/chrome.exe', headless=True)
                page = browser.new_page(viewport={'width': 1440, 'height': 1150})
                page.on('pageerror', lambda error: errors.append(str(error)))
                for number, (fields, title) in enumerate([
                        ({'source': 'download', 'request_mode': 'anonymous_fallback'}, '不带 Cookie 重试时遇到机器人验证'),
                        ({'http_status': 403}, '登录检查被 YouTube 拒绝（HTTP 403）'),
                        ({'verification_reason': 'redirect'}, '登录检查跳转到了其他页面'),
                        ({}, '上次登录或访问验证未通过')]):
                    save({'state': 'verification_required', 'checked_at': 1793936259, **fields})
                    page.goto(f'http://127.0.0.1:{server.server_port}/?verification={number}#settings')
                    page.get_by_role('tab', name='运行诊断').click()
                    notice = page.locator('[data-concurrency-panel=settings] [data-risk-code=bot_check]')
                    expect(notice).to_be_visible(timeout=12000)
                    expect(notice).to_contain_text(title, timeout=12000)
                    expect(notice).to_contain_text('历史记录')
                    expect(notice).to_contain_text('失败记录时间')
                    expect(notice).to_contain_text('HTTP 状态：403' if fields.get('http_status') == 403 else 'HTTP 状态：未记录')
                    expect(notice).not_to_contain_text('Cloudflare')
                    notice.evaluate("node => node.scrollIntoView({block: 'center'})")
                    if fields.get('request_mode') == 'anonymous_fallback':
                        expect(notice).to_contain_text('Video unavailable')
                        expect(notice).to_contain_text('不能证明已上传的 Cookie 失效')
                        notice.screenshot(path=str(output / 'anonymous-desktop.png'))
                        page.set_viewport_size({'width': 390, 'height': 844})
                        page.locator('#sidebar').evaluate('node => Promise.all(node.getAnimations().map(a => a.finished))')
                        notice.evaluate("node => node.scrollIntoView({block: 'center'})")
                        assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth'), 'mobile horizontal overflow'
                        notice.screenshot(path=str(output / 'anonymous-mobile.png'))
                        page.set_viewport_size({'width': 1440, 'height': 1150})
                    else:
                        filename = 'http-403.png' if fields.get('http_status') == 403 else 'redirect.png' if fields else 'legacy.png'
                        notice.screenshot(path=str(output / filename))
                # Let the normal page poll replace the historical failure after a successful check.
                save({'state': 'valid', 'checked_at': time.time(), 'expires_at': time.time()+300})
                expect(page.locator('[data-concurrency-panel=settings] [data-risk-code=auth_valid]')).to_be_visible(timeout=12000)
                expect(notice).not_to_be_visible()
                assert not errors, errors
                browser.close()
        finally:
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()
            store.db.engine.dispose()
    print(json.dumps({'scenarios': ['anonymous', '403', 'redirect', 'legacy', 'recovery'],
                      'mobile_overflow': False, 'page_errors': errors, 'output': str(output)}, ensure_ascii=False))


if __name__ == '__main__':
    main()
