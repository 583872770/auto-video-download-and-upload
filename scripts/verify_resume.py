"""Exercise delayed system resume and paused-instance feedback on both pages."""
from pathlib import Path
import sys
import tempfile
import threading

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from playwright.sync_api import expect, sync_playwright
from werkzeug.serving import make_server

from app import create_app
from scripts.verify_jobs import QuietHandler
from webapp.browser import browser_options


def main():
    output = Path('.cache/validation/resume')
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        config = root / 'config.yaml'
        config.write_text('path: videos\nproxy_mode: direct\ndownload_methods: [downmp3]\n', encoding='utf-8')
        app = create_app(config, root / 'state', start_worker=False, role='hybrid',
                         instance_id='fixture', instance_url='http://127.0.0.1:1',
                         database_url='sqlite:///' + (root / 'shared.db').as_posix())
        store = app.extensions['task_store']
        service = app.extensions['execution_control']
        release, entered = threading.Event(), threading.Event()

        def slow_resume():
            entered.set()
            if not release.wait(15):
                raise ValueError('Fixture resume timed out')

        service.prepare_resume = slow_resume
        server = make_server('127.0.0.1', 0, app, threaded=True, request_handler=QuietHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f'http://127.0.0.1:{server.server_port}'
        errors = []
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True, **browser_options(p.chromium))
                page = browser.new_page(viewport={'width':1440, 'height':1050})
                page.on('pageerror', lambda error: errors.append(str(error)))
                for path, selector, note in [('/', '#download-queue-start', '#download-execution-status'),
                                              ('/jobs', '[data-system-resume]', '#system-note')]:
                    store.set_downloads_paused(True)
                    release.clear()
                    entered.clear()
                    page.goto(base + path)
                    button = page.locator(selector)
                    expect(button).to_be_enabled()
                    button.click()
                    expect(button).to_have_text('正在恢复…')
                    expect(button).to_be_disabled()
                    expect(page.locator(note)).to_contain_text('正在恢复系统领取')
                    assert entered.wait(3)
                    page.screenshot(path=str(output / ('home-pending.png' if path == '/' else 'jobs-pending.png')))
                    release.set()
                    expect(page.locator(note)).to_contain_text('系统已恢复领取')
                    expect(page.locator(note)).to_contain_text('在线下载实例均已暂停')
                    expect(page.locator(note + ' a[href="/#instances"]')).to_be_visible()
                    assert not store.downloads_paused()
                    assert store.fleet.row()['paused']
                    page.screenshot(path=str(output / ('home-result.png' if path == '/' else 'jobs-result.png')))
                    # Follow the real recovery link, including navigation within the homepage.
                    page.locator(note + ' a[href="/#instances"]').click()
                    expect(page.locator('#instances-view')).to_be_visible()
                    expect(page.locator('#overview-view')).to_be_hidden()
                    expect(page.locator('#fleet-nav')).to_have_class('nav-item active')
                    if path == '/':
                        page.go_back()
                        expect(page.locator('#overview-view')).to_be_visible()
                        page.go_forward()
                        expect(page.locator('#instances-view')).to_be_visible()
                    # An environment failure must stay paused and show the reason; retry remains available.
                    store.fleet.check_local = lambda policy: ({'error':'Fixture environment needs repair'}, 409)
                    page.locator('[data-instance-action="resume"]').click()
                    expect(page.locator('#toasts')).to_contain_text('Fixture environment needs repair')
                    expect(page.locator('[data-instance-action="resume"]')).to_be_enabled()
                    assert store.fleet.row()['paused']
                    # Exercise the real instance-control endpoint with a delayed local preflight.
                    release.clear()
                    entered.clear()
                    def slow_preflight(policy):
                        slow_resume()
                        return {'ready': True}, 200
                    store.fleet.check_local = slow_preflight
                    resume = page.locator('[data-instance-action="resume"]')
                    resume.click()
                    expect(resume).to_have_text('正在检查并恢复…')
                    expect(resume).to_be_disabled()
                    assert entered.wait(3)
                    page.evaluate('loadFleet()')  # Polling must not replace the in-flight button.
                    expect(resume).to_have_text('正在检查并恢复…')
                    expect(resume).to_be_disabled()
                    release.set()
                    expect(page.locator('#fleet-instances .count-pill')).to_have_text('在线')
                    assert not store.fleet.row()['paused']
                    page.locator('[data-view="overview"]').first.click()
                    expect(page.locator('#download-execution-status')).not_to_contain_text('在线下载实例均已暂停')
                    store.fleet.control('fixture', {'action':'drain'})
                # A timeout means the result is unknown; it must not claim cancellation or success.
                store.set_downloads_paused(True)
                release.clear()
                entered.clear()
                page.add_init_script('AbortSignal.timeout = () => { const controller = new AbortController(); setTimeout(() => controller.abort(new DOMException("test timeout", "TimeoutError")), 300); return controller.signal; };')
                page.goto(base + '/jobs')
                page.locator('[data-system-resume]').click()
                expect(page.locator('#system-note')).to_contain_text('结果尚未确认')
                expect(page.locator('[data-system-resume]')).to_be_enabled()
                assert entered.wait(3)
                release.set()
                page.screenshot(path=str(output / 'timeout-feedback.png'))
                assert not errors, errors
                browser.close()
        finally:
            release.set()
            server.shutdown()
            thread.join(timeout=3)
            server.server_close()
    print('Resume browser verification passed: delayed requests, both recovery links, history navigation, instance preflight failure/retry, polling and timeout feedback.')


if __name__ == '__main__':
    main()
