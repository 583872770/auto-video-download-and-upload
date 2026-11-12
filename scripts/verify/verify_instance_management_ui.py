"""Exercise reciprocal consoles, failed switches and late responses in Chromium."""
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
from webapp.browser import browser_options
from webapp.cookie_profiles import CookieProfiles
from webapp.database import instances
from webapp.deployment import DeploymentGateway


class QuietHandler(WSGIRequestHandler):
    def log(self, *args, **kwargs):
        pass


def main():
    output = Path(__file__).resolve().parents[2] / '.runtime/cache-archive-20261111/instance-management-ui'
    output.mkdir(parents=True, exist_ok=True)
    environment = {k: v for k, v in os.environ.items() if not k.startswith(('VIDEO_', 'MYSQL_', 'YT5S_', 'REDIS_'))}
    with tempfile.TemporaryDirectory(prefix='instance-management-') as temporary, patch.dict(os.environ, environment, clear=True):
        root = Path(temporary)
        apps, servers, threads, addresses, gateways = {}, [], [], {}, {}
        stop = threading.Event()
        try:
            for node in ('alpha', 'beta', 'gamma'):
                base = root / node
                base.mkdir()
                config = base / 'config.yaml'
                config.write_text(f'path: videos-{node}\nproxy_mode: direct\nmin_free_gb: 0\n'
                                  'download_methods: [yt_dlp]\n', encoding='utf-8')
                app = create_app(config, base / 'state', start_worker=False, role='hybrid',
                    instance_id=node, instance_name=node, database_url='sqlite:///' + (root / 'shared.sqlite3').as_posix())
                apps[node] = app
                gateway = DeploymentGateway(config, base / 'state', load_environment=False)
                gateway.profile.update(instance_id=node, instance_name=node, mysql_host='mysql.fixture.test',
                    mysql_port=3306, mysql_database='shared', mysql_user='fixture', mysql_password='fixture-secret',
                    redis_url='redis://:redis-fixture-secret@redis.fixture.test:6379/0')
                gateway.active = app; gateway.running_profile = dict(gateway.profile)
                app.extensions['deployment_gateway'] = gateway
                gateways[node] = gateway
                library = CookieProfiles(base, app.extensions['task_store'])
                cookie = f'# HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tSID\tfixture-{node}\n'.encode()
                library.add(f'{node}-cookie', cookie)
                server = make_server('127.0.0.1', 0, app, threaded=True, request_handler=QuietHandler)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start(); servers.append(server); threads.append(thread)
                addresses[node] = f'http://127.0.0.1:{server.server_port}'
                with app.extensions['task_store'].connect() as db:
                    db.execute(instances.update().where(instances.c.id == node).values(url=addresses[node]))

            def heartbeat():
                while not stop.wait(5):
                    for app in apps.values():
                        app.extensions['fleet'].beat()
            beat = threading.Thread(target=heartbeat, daemon=True); beat.start()
            errors = []
            with sync_playwright() as pw, patch('webapp.managed_deployment.test_connections'):
                browser = pw.chromium.launch(headless=True, **browser_options(pw.chromium))
                page = browser.new_page(viewport={'width': 1440, 'height': 1100})
                page.on('pageerror', lambda error: errors.append(str(error)))

                def ready(node):
                    page.wait_for_function('(id) => state.instanceId === id && state.instanceReady && !state.instanceLoading', arg=node)
                    expect(page.locator('#settings-view')).not_to_have_attribute('inert', '')

                pairs = []
                for source in apps:
                    for target in apps:
                        if source == target:
                            continue
                        page.goto(addresses[source] + f'/?instance_id={target}#settings')
                        ready(target)
                        expect(page.locator('#fleet-target-name')).to_have_text(target)
                        expect(page.locator('#fleet-entry-name')).to_have_text(source)
                        expect(page.locator('#fleet-target-status')).to_contain_text('在线')
                        expect(page.locator('#settings-form button[type=submit]')).to_contain_text(f'「{target}」')
                        page.locator('#settings-tab-storage').click()
                        value = f'{source}-manages-{target}'
                        page.locator('[name=path]').fill(value)
                        page.locator('#settings-form button[type=submit]').click()
                        expect(page.locator('#settings-save-state')).to_contain_text(f'「{target}」系统设置已保存')
                        actual = apps[target].extensions['fleet'].local_settings()['config']
                        assert actual['path'] == value
                        config = page.evaluate('() => api("/api/config")')
                        assert config['instance_id'] == target
                        assert config['config_path'] == str(root / target / 'config.yaml')
                        assert config['media_directory'] == str(root / target / value)
                        page.locator('[data-view=accounts].nav-item').click()
                        page.locator('[data-account-tab=library]').click()
                        page.locator('#account-instance').select_option(target)
                        expect(page.locator('#youtube-cookie-list')).to_contain_text(f'{target}-cookie')
                        page.locator('#account-rows [data-credential-open]').first.click()
                        page.locator('[data-account-detail-tab=schedule]').click()
                        expect(page.locator('#account-detail-dialog')).to_be_visible()
                        expect(page.locator('[data-account-detail-pane=schedule]')).to_be_visible()
                        expect(page.locator('#account-detail-error')).to_be_hidden()
                        page.locator('#account-detail-dialog [data-close]').click()
                        page.locator('[data-view=deployment]').click()
                        form = page.locator('#instance-deployment-form')
                        expect(form).to_be_visible()
                        expect(form.locator('[name=instance_id]')).to_have_value(target)
                        expect(form.locator('button[type=submit]')).to_contain_text(f'「{target}」')
                        expect(form.locator('[name=mysql_password]')).to_have_value('')
                        expect(form.locator('[name=redis_url]')).to_have_value('')
                        name = f'{source}-deployment-{target}'
                        form.locator('[name=instance_name]').fill(name)
                        page.locator('#fleet-selection').select_option(source)
                        expect(page.locator('#fleet-selection')).to_have_value(target)
                        page.locator('#instance-deployment-test').click()
                        expect(page.locator('#instance-deployment-result')).to_contain_text('连接成功')
                        form.locator('button[type=submit]').click()
                        expect(page.locator('#instance-deployment-result')).to_contain_text('已保存')
                        expect(page.locator('#instance-deployment-state')).to_contain_text('等待重启')
                        assert gateways[target].profile['instance_name'] == name
                        assert gateways[target].profile['mysql_password'] == 'fixture-secret'
                        expect(form.locator('[name=mysql_password]')).to_have_value('')
                        pairs.append([source, target])

                page.screenshot(path=str(output / 'deployment-desktop.png'), full_page=True, animations='disabled')
                page.set_viewport_size({'width': 390, 'height': 844})
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                assert page.locator('#fleet-selection').bounding_box()['width'] > 270
                assert page.locator('#fleet-search').bounding_box()['width'] > 270
                page.screenshot(path=str(output / 'deployment-mobile.png'), full_page=True, animations='disabled')
                page.set_viewport_size({'width': 1440, 'height': 1100})

                page.goto(addresses['alpha'] + '/?instance_id=alpha#settings'); ready('alpha')
                # Filtering stays local and retains a nonmatching target across polls.
                search_requests = []
                page.on('request', lambda request: search_requests.append(request.url))
                page.locator('#fleet-search').fill('BETA')
                expect(page.locator('#fleet-search-result')).to_contain_text('找到 1 / 3')
                expect(page.locator('#fleet-selection')).to_have_value('alpha')
                assert page.locator('#fleet-selection option').count() == 2
                assert not any('BETA' in url for url in search_requests)
                page.wait_for_timeout(2300)
                expect(page.locator('#fleet-search')).to_have_value('BETA')
                expect(page.locator('#fleet-selection')).to_have_value('alpha')
                expect(page.locator('#fleet-entry-name')).to_have_text('alpha')
                page.locator('#fleet-selection').select_option('beta'); ready('beta')
                expect(page.locator('#fleet-target-name')).to_have_text('beta')
                expect(page.locator('#fleet-entry-name')).to_have_text('alpha')
                page.locator('#fleet-search').fill('no-matching-instance')
                expect(page.locator('#fleet-search-result')).to_contain_text('找到 0 / 3')
                expect(page.locator('#fleet-selection')).to_have_value('beta')
                page.locator('#fleet-search').fill('')
                # Status labels distinguish an offline server from a stopped worker.
                def fleet_statuses(route):
                    response = route.fetch()
                    data = response.json()
                    for item in data['items']:
                        if item['id'] == 'beta': item['online'] = False
                        if item['id'] == 'gamma': item.update(online=True, paused=True, running=0)
                    route.fulfill(response=response, json=data)
                page.route('**/api/instances*', fleet_statuses)
                page.evaluate('() => loadFleet()')
                expect(page.locator('#fleet-target-status')).to_have_text('离线')
                expect(page.locator('#fleet-selection option[value=beta]')).to_contain_text('离线')
                expect(page.locator('#fleet-selection option[value=gamma]')).to_contain_text('已停止')
                expect(page.locator('#fleet-selection')).to_have_value('beta')
                page.unroute('**/api/instances*', fleet_statuses)
                page.locator('#fleet-selection').select_option('alpha'); ready('alpha')
                page.locator('#settings-tab-storage').click()
                original = apps['beta'].extensions['fleet'].local_settings()['config']['path']

                def fail_beta(route):
                    if route.request.method == 'GET' and route.request.headers.get('x-instance-id') == 'beta':
                        route.fulfill(status=503, json={'error': 'fixture peer unavailable'})
                    else:
                        route.continue_()
                page.route('**/api/config', fail_beta)
                page.locator('#fleet-selection').select_option('beta')
                expect(page.locator('#fleet-reload')).to_be_visible()
                expect(page.locator('#fleet-selection-note')).to_contain_text('fixture peer unavailable')
                expect(page.locator('#settings-view')).to_have_attribute('inert', '')
                denied = page.evaluate('() => api("/api/config", {method:"PUT",body:{path:"wrong-target"}}).then(()=>false,e=>e.message)')
                assert '尚未就绪' in denied
                assert apps['beta'].extensions['fleet'].local_settings()['config']['path'] == original
                page.unroute('**/api/config', fail_beta)
                page.locator('#fleet-reload').click(); ready('beta')
                expect(page.locator('[name=path]')).to_have_value(original)

                # A workbench failure must not lock otherwise valid system settings.
                def fail_workbench(route):
                    route.fulfill(status=503, json={'error': 'fixture workbench unavailable'})
                page.route('**/api/jobs/workbench*', fail_workbench)
                page.locator('#fleet-selection').select_option('alpha'); ready('alpha')
                expect(page.locator('[name=path]')).to_be_editable()
                page.unroute('**/api/jobs/workbench*', fail_workbench)

                # A -> B -> A cannot make a response from the first A current again.
                page.locator('#fleet-selection').select_option('alpha'); ready('alpha')
                held = []
                def hold(route):
                    if not held and route.request.headers.get('x-instance-id') == 'alpha':
                        held.append(route)
                    else:
                        route.continue_()
                page.route('**/api/youtube/cookies', hold)
                page.evaluate('() => { window.oldCookieRead = loadCookieProfiles().then(()=>"applied",e=>e.name); }')
                page.wait_for_timeout(100)
                assert held
                page.locator('#fleet-selection').select_option('beta'); ready('beta')
                page.locator('#fleet-selection').select_option('alpha'); ready('alpha')
                for route in held:
                    route.fulfill(status=200, json={'items': [], 'cookie_path': 'stale-cookie', 'pool': None})
                page.unroute('**/api/youtube/cookies', hold)
                assert page.evaluate('window.oldCookieRead') == 'AbortError'
                assert page.evaluate('state.config.youtube_cookie') != 'stale-cookie'

                # Drafted proxy changes must not be silently carried to another server.
                page.goto(addresses['alpha'] + '/?instance_id=alpha#clash'); ready('alpha')
                page.locator('#clash-subscription-url').fill('https://example.test/pending')
                page.locator('#fleet-selection').select_option('beta')
                expect(page.locator('#fleet-selection')).to_have_value('alpha')
                page.locator('#clash-subscription-url').fill('')
                page.locator('#fleet-selection').select_option('beta'); ready('beta')

                # An outstanding mutation must finish before the target can change.
                held_write = []
                def hold_write(route):
                    held_write.append(route)
                page.route('**/api/disk/cleanup', hold_write)
                page.evaluate('() => { window.pendingWrite = api("/api/disk/cleanup", {method:"POST"}); }')
                page.wait_for_timeout(100)
                assert held_write
                page.locator('#fleet-selection').select_option('alpha')
                expect(page.locator('#fleet-selection')).to_have_value('beta')
                held_write[0].fulfill(status=200, json={'message': 'fixture finished'})
                page.evaluate('window.pendingWrite')
                page.unroute('**/api/disk/cleanup', hold_write)

                # Register switch/retry handlers even if the initial list request fails.
                page.evaluate('localStorage.clear()')
                def fail_list(route):
                    route.fulfill(status=503, json={'error': 'fixture registry unavailable'})
                page.route('**/api/instances*', fail_list)
                page.goto(addresses['alpha'] + '/?fixture=initial-list#settings')
                # Hold background recovery so this scenario exercises the
                # explicit retry button rather than racing the 2-second poll.
                page.evaluate('() => { window.retryLoadFleet = loadFleet; loadFleet = options => options?.poll ? Promise.resolve() : retryLoadFleet(options); }')
                expect(page.locator('#fleet-reload')).to_be_visible()
                expect(page.locator('#fleet-reload')).to_be_enabled()
                expect(page.locator('#settings-view')).to_have_attribute('inert', '')
                page.unroute('**/api/instances*', fail_list)
                page.locator('#fleet-reload').click(); ready('alpha')
                page.evaluate('() => { loadFleet = window.retryLoadFleet; }')
                page.locator('#fleet-selection').select_option('gamma'); ready('gamma')

                page.goto(addresses['alpha'] + '/?instance_id=missing#settings')
                expect(page.locator('#fleet-selection')).to_have_value('missing')
                expect(page.locator('#fleet-selection-note')).to_contain_text('无法读取')
                expect(page.locator('#fleet-target-name')).to_have_text('missing')
                expect(page.locator('#fleet-target-status')).to_have_text('未登记')
                expect(page.locator('#fleet-entry-name')).to_have_text('alpha')
                page.wait_for_timeout(2300)
                expect(page.locator('#fleet-selection')).to_have_value('missing')
                expect(page.locator('#settings-view')).to_have_attribute('inert', '')
                page.screenshot(path=str(output / 'missing-peer-desktop.png'), full_page=True)
                page.set_viewport_size({'width': 390, 'height': 844})
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                page.screenshot(path=str(output / 'missing-peer-mobile.png'), full_page=True)
                assert not errors, errors
                result = {'pairs': pairs, 'failed_switch_blocked': True, 'retry': True,
                          'late_response_rejected': True, 'proxy_draft_protected': True,
                          'missing_peer_retained': True, 'initial_list_retry': True,
                          'settings_survive_workbench_error': True, 'mutation_switch_blocked': True,
                          'deployment_reciprocal_save': True, 'deployment_draft_protected': True,
                          'entry_target_labels': True, 'target_save_labels': True,
                          'search_preserves_selection': True, 'search_survives_poll': True,
                          'offline_and_stopped_labels': True, 'mobile_picker_width': True,
                          'mobile': True, 'page_errors': errors}
                (output / 'results.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
                print(json.dumps(result), flush=True)
                browser.close()
        finally:
            stop.set()
            if 'beat' in locals(): beat.join(timeout=5)
            for server in servers: server.shutdown(); server.server_close()
            for thread in threads: thread.join(timeout=5)


if __name__ == '__main__':
    main()
