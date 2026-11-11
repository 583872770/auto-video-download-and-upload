"""Exercise automatic routing settings in a real browser and an isolated DB."""
from pathlib import Path
import sys
import tempfile
import threading

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from playwright.sync_api import expect, sync_playwright
from werkzeug.serving import make_server

from app import create_app
from scripts.verify_jobs import QuietHandler
from scripts.settings_roundtrip_checks import reveal_setting
from webapp.browser import browser_options


def main():
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        config = root / 'config.yaml'
        config.write_text('path: videos\nproxy_mode: direct\ndownload_methods: [bergung]\n', encoding='utf-8')
        app = create_app(config, root / 'state', start_worker=False, role='hybrid',
                         instance_id='auto-test', database_url='sqlite:///' + (root / 'test.db').as_posix())
        server = make_server('127.0.0.1', 0, app, threaded=True, request_handler=QuietHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        errors, submitted = [], []
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True, **browser_options(pw.chromium))
                page = browser.new_page(viewport={'width':1440, 'height':1050})
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.on('request', lambda req: submitted.append(req.post_data_json)
                        if req.method == 'PUT' and req.url.endswith('/api/config') else None)
                page.goto(f'http://127.0.0.1:{server.server_port}/#settings')
                form = page.locator('#settings-form')
                expect(form.locator('[name=scheduler_mode]')).to_have_value('ordered_fallback')
                reveal_setting(page, form.locator('[name=scheduler_mode]')).select_option('auto')
                page.locator('#settings-tab-download').click()
                expect(page.locator('#download-method')).to_contain_text('自动候选')
                expect(page.locator('#download-method .provider-order').first).to_be_hidden()
                expect(form.locator('[name=throughput_mode]')).to_have_value('manual')
                expect(form.locator('[name=throughput_mode]')).to_be_enabled()
                page.locator('#settings-tab-performance').click()
                page.locator('#cobalt-catalog-add').click()
                expect(page.locator('.channel-entry')).to_have_count(1)
                form.locator('[name=builtin_transfer_limit]').fill('0')
                form.locator('[name=builtin_resolver_limit]').fill('0')
                expect(page.locator('#concurrency-config-note')).to_contain_text('内置渠道上限 不限制')
                page.locator('#cobalt-catalog-add').click()
                expect(page.locator('.channel-entry')).to_have_count(1)
                with page.expect_response(lambda response: response.request.method == 'PUT'
                                          and response.url.endswith('/api/config')) as response:
                    form.locator('button[type=submit]').click()
                assert response.value.status == 200, response.value.text()
                expect(page.locator('#toasts')).to_contain_text('设置已保存')
                assert submitted[-1]['scheduler_mode'] == 'auto', submitted[-1]
                assert submitted[-1]['builtin_transfer_limit'] == submitted[-1]['builtin_resolver_limit'] == '0'
                saved = app.test_client().get('/api/config').json['config']
                assert saved['builtin_transfer_limit'] == saved['builtin_resolver_limit'] == 0
                channel = submitted[-1]['download_channels'][0]
                assert channel['verification'] == 'auto' and channel['task_limit'] == 4, channel
                expect(form.locator('button[type=submit]')).to_be_enabled()
                # An unrelated save must preserve all channel options without
                # resending an old channel list from this browser tab.
                reveal_setting(page, form.locator('[name=bergung_quality]')).select_option('720')
                with page.expect_response(lambda response: response.request.method == 'PUT'
                                          and response.url.endswith('/api/config')) as response:
                    form.locator('button[type=submit]').click()
                assert response.value.status == 200, response.value.text()
                assert submitted[-1] == {'bergung_quality':'720'}, submitted[-1]
                page.reload()
                expect(form.locator('[name=scheduler_mode]')).to_have_value('auto')
                expect(form.locator('[name=builtin_transfer_limit]')).to_have_value('0')
                expect(form.locator('[name=builtin_resolver_limit]')).to_have_value('0')
                diagnostic_html = page.evaluate("""() => concurrencyHTML(
                    state.fleet.items.find(item => item.id === 'auto-test').concurrency)""")
                assert '解析上限 不限制' in diagnostic_html and 'null' not in diagnostic_html, diagnostic_html
                expect(page.locator('[data-channel-field=verification]')).to_have_value('auto')
                expect(page.locator('[data-channel-field=task_limit]')).to_have_value('4')
                reveal_setting(page, form.locator('[name=scheduler_mode]')).select_option('ordered_fallback')
                page.locator('#settings-tab-download').click()
                expect(page.locator('#download-method .provider-order').first).to_be_visible()
                expect(form.locator('[name=throughput_mode]')).to_be_enabled()
                output = Path(__file__).resolve().parents[1] / '.cache/validation/cobalt'
                output.mkdir(parents=True, exist_ok=True)
                reveal_setting(page, form.locator('[name=scheduler_mode]')).select_option('auto')
                page.screenshot(path=str(output / 'auto-settings.png'), full_page=True)
                assert not errors, errors
                browser.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)
    print('PASS: automatic candidates, catalog deduplication, settings roundtrip and partial saves.')


if __name__ == '__main__':
    main()
