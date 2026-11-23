"""Check the built reduction switch against isolated settings and resource samples."""
import json
from pathlib import Path
import re
import sys
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from playwright.sync_api import expect, sync_playwright
from werkzeug.serving import make_server
from tests.test_console_auth import ConsoleAuthTests
from scripts.verify.support import QuietHandler
from webapp.browser import browser_options


def main():
    fixture = ConsoleAuthTests()
    fixture.setUp()
    server = None
    output = Path('.runtime/verify/auto-reduce')
    output.mkdir(parents=True, exist_ok=True)
    try:
        fixture.setup_admin()
        server = make_server('127.0.0.1', 0, fixture.app, threaded=True, request_handler=QuietHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        url = f'http://127.0.0.1:{server.server_port}'
        errors = []
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, **browser_options(pw.chromium))
            context = browser.new_context(viewport={'width': 1440, 'height': 1050})
            cookie = fixture.app.config['SESSION_COOKIE_NAME']
            context.add_cookies([dict(name=cookie, value=fixture.client.get_cookie(cookie).value, url=url)])
            page = context.new_page()
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.goto(url + '/settings#performance')
            switch = page.get_by_role('switch', name='自动降低下载进程上限', exact=True)
            save = page.locator('.savebar').get_by_role('button', name=re.compile('保存到'))
            expect(switch).to_be_checked()
            expect(save).to_be_disabled()

            def persist(value):
                with page.expect_response(lambda r: r.url.endswith('/api/config') and r.request.method == 'PUT') as response:
                    save.click()
                assert response.value.status == 200, response.value.text()
                assert response.value.request.post_data_json['download_auto_reduce'] is value
                expect(save).to_be_disabled()
                expect(page.get_by_role('status')).to_contain_text('下一次资源采样')
                assert fixture.client.get('/api/config').json['config']['download_auto_reduce'] is value

            switch.uncheck()
            expect(save).to_be_enabled()
            persist(False)
            page.reload()
            expect(switch).not_to_be_checked()
            for width in (1440, 390):
                page.set_viewport_size({'width': width, 'height': 1000})
                expect(switch).to_be_visible()
                if width == 390:
                    expect(page.locator('.sidebar')).to_have_css('transform', 'matrix(1, 0, 0, 1, -224, 0)')
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), width
                page.screenshot(path=str(output / f'settings-{width}.png'), full_page=True, animations='disabled')
            switch.focus()
            switch.press('Space')
            expect(switch).to_be_checked()
            persist(True)

            # Use the real status shape, with an isolated disabled runtime sample.
            status = fixture.client.get('/api/status').json
            status['concurrency'].update(online=True, sample_fresh=True, time=time.time(),
                worker_control=dict(auto_reduce_enabled=False, dynamic_limit=24, normal_limit=24,
                    configured_limit=24, used=20, resource_limited=False, recovery=None, last_reduction=None,
                    measurements=dict(cpu_percent=98, memory_available_mb=4096, io_pressure=22,
                                      io_pressure_source='cgroup')))
            page.route('**/api/status', lambda route: route.fulfill(json=status))
            page.goto(url + '/workbench')
            panel = page.get_by_role('region', name='当前实例传输并发')
            expect(panel).to_contain_text('自动降低下载进程上限已关闭')
            expect(panel).not_to_contain_text('下次可调整检查')
            page.screenshot(path=str(output / 'capacity-disabled-390.png'), full_page=True)
            browser.close()
        assert not errors, errors
        print(json.dumps(dict(ok=True, browser_errors=errors, screenshots=str(output))))
    finally:
        if server:
            server.shutdown()
            server.server_close()
        fixture.doCleanups()


if __name__ == '__main__':
    main()
