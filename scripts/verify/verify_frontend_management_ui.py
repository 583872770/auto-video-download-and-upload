"""Vue management workflows against isolated fleet HTTP servers and SQLite.

External database probes, Mihomo and browser login are stubbed; routing,
authentication, versions, forwarding, persistence, CSRF and billing APIs are real.
"""
import base64
from contextlib import ExitStack
from io import BytesIO
import os
from pathlib import Path
import re
import sys
import tempfile
import threading
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from flask import Flask
from playwright.sync_api import expect, sync_playwright
from werkzeug.serving import make_server
from app import create_app
from scripts.verify.verify_jobs import QuietHandler
from webapp.account_routes import PREFIX
from webapp.browser import browser_options
from webapp.database import instances
from webapp.deployment import DeploymentGateway
from webapp.fleet_routes import rpc as instance_rpc


def token(client):
    return {'X-CSRF-Token': re.search(r'name="csrf-token" content="([^"]+)"', client.get('/login').text)[1]}


def main():
    output = Path('.runtime/verify/frontend-management')
    output.mkdir(parents=True, exist_ok=True)
    environment = {k: v for k, v in os.environ.items() if not k.startswith(('VIDEO_', 'MYSQL_', 'YT5S_', 'REDIS_'))}
    environment['VIDEO_ACCOUNT_ENCRYPTION_KEY'] = base64.urlsafe_b64encode(b'm' * 32).decode()
    with tempfile.TemporaryDirectory(prefix='vue-management-') as temporary, patch.dict(os.environ, environment, clear=True), ExitStack() as stack:
        root = Path(temporary)
        apps, servers, gateways, proxy_states = {}, [], {}, {}

        def serve(app):
            server = make_server('127.0.0.1', 0, app, threaded=True, request_handler=QuietHandler)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            servers.append(server)
            return f'http://127.0.0.1:{server.server_port}'

        addresses = {}
        for identity in ('alpha', 'beta'):
            base = root / identity
            base.mkdir()
            config = base / 'config.yaml'
            config.write_text(f'path: videos-{identity}\nproxy_mode: direct\nmin_free_gb: 0\ndownload_methods: [yt_dlp]\n', encoding='utf-8')
            app = create_app(config, base / 'state', start_worker=False, role='hybrid', instance_id=identity,
                             instance_name=identity, database_url='sqlite:///' + (root / 'shared.db').as_posix())
            apps[identity] = app
            gateway = DeploymentGateway(config, base / 'state', load_environment=False)
            gateway.profile.update(instance_id=identity, instance_name=identity, mysql_host=f'{identity}.mysql.test',
                                   mysql_port=3306, mysql_database='shared', mysql_user='fixture', mysql_password='mysql-fixture-secret',
                                   redis_url='redis://:redis-fixture-secret@cache.test:6379/1')
            gateway.active = app
            gateway.running_profile = dict(gateway.profile)
            app.extensions['deployment_gateway'] = gateway
            gateways[identity] = gateway
            addresses[identity] = serve(app)
            with app.extensions['task_store'].connect() as db:
                db.execute(instances.update().where(instances.c.id == identity).values(url=addresses[identity]))
            # Deterministic local proxy provider, with the actual Flask APIs and fleet forwarding.
            manager = app.extensions['clash']
            fake = {'installed': True, 'running': False, 'selected': f'{identity}-one',
                    'proxy_url': 'http://127.0.0.1:7890', 'error': '', 'active_subscription_id': '',
                    'subscriptions': [], 'notices': [], 'testing': {'running': False},
                    'nodes': [{'name': f'{identity}-{name}', 'type': 'ss', 'delay': 20, 'cooling_until': 0} for name in ('one', 'two')],
                    'rotation': {'id': '', 'enabled': False, 'nodes': [], 'every': 1, 'count_failures': True, 'start_index': 0}}
            proxy_states[identity] = fake
            stack.enter_context(patch.object(manager, 'status', side_effect=lambda fake=fake: {**fake, 'rotation': dict(fake['rotation'])}))
            stack.enter_context(patch.object(manager, 'configure_rotation', side_effect=lambda data, fake=fake: fake.update(rotation={**data, 'id': '', 'start_index': 0})))
            stack.enter_context(patch.object(manager, 'test_youtube', return_value={'ms': 12, 'node': identity}))
            stack.enter_context(patch.object(manager, 'select', side_effect=lambda name, fake=fake: fake.update(selected=name)))
            stack.enter_context(patch.object(manager, 'test_delays', return_value=None))
            def subscription(url, *, identity=None, name=None, activate=True, manager=manager, fake=fake):
                identity=identity or 'fixture-subscription'
                entry={'id':identity,'name':name,'url':url or 'https://subscription.test/fixture'}
                manager.config['subscriptions']=[entry]
                manager.config['active_subscription_id']=identity
                fake.update(active_subscription_id=identity,subscriptions=[{'id':identity,'name':name,'host':'subscription.test','node_count':2,'updated_at':''}])
                return 2
            stack.enter_context(patch.object(manager, 'import_subscription', side_effect=subscription))

        stack.enter_context(patch('webapp.managed_deployment.test_connections'))
        stack.enter_context(patch('webapp.deployment.test_mysql'))
        stack.enter_context(patch('webapp.fleet_routes.rpc', side_effect=lambda fleet, target, path, **kwargs:
                                   {'ready': True} if path == '/api/fleet/preflight' else instance_rpc(fleet, target, path, **kwargs)))
        client = apps['alpha'].test_client()
        headers = token(client)
        response = client.post('/api/auth/setup', headers=headers, json={'username': 'admin', 'password': 'fixture-admin-password',
                              'bootstrap_key': apps['alpha'].extensions['console_auth'].bootstrap_key})
        assert response.status_code == 201, response.json
        headers = token(client)
        accounts = []
        for number, owner in enumerate(('alpha', 'alpha', 'beta')):
            response = client.post(PREFIX, headers=headers, json={'email': f'account{number}@example.com', 'password': 'account-fixture-secret',
                                   'extra': 'private-extra', 'instance_id': owner, 'note': 'Initial note'})
            assert response.status_code == 201, response.json
            identity = response.json['id']
            cookie = f'# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tSID\tfixture-{number}\n'.encode()
            response = client.post(PREFIX + '/' + identity + '/upload', headers=headers,
                                   data={'cookie': (BytesIO(cookie), 'fixture.txt'), 'confirmed': 'true', 'version': '1'})
            assert response.status_code == 201, response.json
            accounts.append(identity)

        # A genuine execution session, no network download.
        store = apps['alpha'].extensions['task_store']
        settings = apps['alpha'].extensions['fleet'].local_settings()
        store.set_download_settings(settings['config'], root / 'alpha', store.directory / 'clash')
        store.create('download', {'base': str(root / 'alpha'), 'urls': ['https://youtu.be/npNnbj46_ik']})
        store.activate_download_lists()
        store.fleet.control('alpha', {'action': 'resume'})
        task = store.claim(store.owner)
        assert task, 'Session fixture did not claim'
        final = root / 'alpha' / 'fixture.mp4'
        final.write_bytes(b'x' * 1234)
        store.statistics.record_file(task['id'], final, 'npNnbj46_ik', 'Fixture video')
        store.finish(task['id'], result={}, worker_id=store.owner)
        store.fleet.control('alpha', {'action': 'drain'})

        # A separate setup gateway must work without a business application/database.
        recovery = DeploymentGateway(root / 'recovery.yaml', root / 'recovery', load_environment=False)
        recovery.profile.update(instance_id='recovery', instance_name='Recovery', mysql_host='fixture.mysql.test', mysql_port=3306,
                                mysql_database='shared', mysql_user='fixture', mysql_password='recovery-secret', web_hosts='127.0.0.1')
        recovery.startup_error=''
        recovery_url = serve(recovery)
        errors, requests = [], []
        stop=threading.Event()
        def heartbeat():
            while not stop.wait(5):
                for app in apps.values(): app.extensions['fleet'].beat()
        beat=threading.Thread(target=heartbeat,daemon=True)
        beat.start()
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True, **browser_options(pw.chromium))
                context = browser.new_context(viewport={'width': 1440, 'height': 1100}, reduced_motion='reduce')
                name = apps['alpha'].config['SESSION_COOKIE_NAME']
                context.add_cookies([{'name': name, 'value': client.get_cookie(name).value, 'url': addresses['alpha']}])
                page = context.new_page()
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.on('console', lambda message: errors.append(message.text) if message.type=='error' and ('TypeError' in message.text or 'Content Security Policy' in message.text) else None)
                page.on('request', lambda request: requests.append(request) if '/api/' in request.url else None)
                page.on('dialog', lambda dialog: dialog.accept())
                base = addresses['alpha']

                page.goto(base + '/?instance_id=alpha#accounts')
                expect(page.get_by_role('heading', name='账号管理', exact=True)).to_be_visible()
                expect(page.get_by_role('row').filter(has_text='account0@example.com')).to_be_visible()
                page.get_by_role('row').filter(has_text='account0@example.com').get_by_role('button', name='详情 / 编辑 / 登录').click()
                detail = page.get_by_role('dialog', name='account0@example.com', exact=True)
                expect(detail.get_by_label('密码', exact=True)).to_have_value('')
                page.get_by_role('button', name='查看附加信息', exact=True).click()
                expect(detail.get_by_label('按需读取的资料')).to_have_value('private-extra')
                detail.get_by_role('button', name='隐藏', exact=True).click()
                expect(detail.get_by_label('按需读取的资料')).to_have_count(0)

                # An optimistic-lock conflict preserves all input and never silently rebases.
                current = client.get(PREFIX + '/' + accounts[0]).json['item']
                response = client.patch(PREFIX + '/' + accounts[0], headers=headers, json={'version': current['version'], 'note': 'Concurrent note'})
                assert response.status_code == 200
                detail.get_by_label('备注', exact=True).fill('Unsaved note')
                detail.get_by_role('button', name='保存账号', exact=True).click()
                expect(detail.get_by_role('alert')).to_contain_text('输入已保留')
                expect(detail.get_by_label('备注', exact=True)).to_have_value('Unsaved note')
                detail.get_by_role('button', name='放弃修改并重新读取', exact=True).click()
                expect(detail.get_by_label('备注', exact=True)).to_have_value('Concurrent note')
                detail.get_by_role('button', name='Cookie 与登录', exact=True).click()
                with page.expect_download() as download:
                    detail.get_by_role('button', name='导出 Cookie', exact=True).click()
                assert 'fixture-0' in Path(download.value.path()).read_text()

                # A different login session cannot be cancelled from this account panel.
                other = {'id': 'other-session', 'state': 'waiting', 'active': True, 'message': 'Other account',
                         'account': {'account_id': accounts[1], 'instance_id': 'alpha', 'version': 1}}
                with patch.object(apps['alpha'].extensions['youtube_login'], 'status', return_value=other):
                    detail.get_by_role('button', name='登录 / 重新登录', exact=True).click()
                    login = page.get_by_role('dialog', name='登录：account0@example.com', exact=True)
                    expect(login).to_contain_text('已有其他登录窗口')
                    expect(login.get_by_role('button', name='取消此会话')).to_have_count(0)
                    login.get_by_role('button', name='开始登录', exact=True).is_disabled()
                    login.get_by_role('button', name='关闭对话框', exact=True).click()
                detail.get_by_role('button', name='关闭对话框', exact=True).click()

                page.get_by_role('button', name='导入账号', exact=True).click()
                importing = page.get_by_role('dialog', name='批量导入账号', exact=True)
                importing.get_by_label('导入文本', exact=True).fill('new@example.com----import-fixture-secret')
                importing.get_by_role('button', name='预览', exact=True).click()
                expect(importing.get_by_role('button', name='确认导入', exact=True)).to_be_enabled()
                assert 'import-fixture-secret' not in importing.inner_text(), 'Import preview leaked a password'
                importing.get_by_label('导入文本', exact=True).fill('changed@example.com----import-fixture-secret')
                expect(importing.get_by_role('button', name='确认导入', exact=True)).to_have_count(0)
                importing.get_by_role('button', name='预览', exact=True).click()
                importing.get_by_role('button', name='确认导入', exact=True).click()
                expect(importing).to_have_count(0)
                expect(page.get_by_role('cell', name='changed@example.com', exact=True)).to_be_visible()

                # Saving pacing leaves membership drafts, and vice versa.
                page.get_by_role('button', name='实例账号池与节奏', exact=True).click()
                assert not errors, errors
                page.get_by_label('启用账号池（至少 2 份 Cookie，仅 yt-dlp）', exact=True).check()
                for checkbox in page.get_by_role('checkbox', name=re.compile('加入账号池 ')).all():
                    checkbox.check()
                interval = page.get_by_label('两次任务启动最小间隔（秒）', exact=False)
                interval.fill('111')
                page.get_by_role('button', name='保存节奏', exact=True).click()
                expect(page.get_by_role('status')).to_contain_text('节奏已保存')
                expect(page.get_by_label('当前实例', exact=True)).to_be_disabled()
                page.get_by_role('button', name='保存账号池', exact=True).click()
                expect(page.get_by_role('status')).to_contain_text('账号池已保存')
                expect(page.get_by_label('当前实例', exact=True)).to_be_enabled()
                expect(interval).to_have_value('111')
                alpha = client.get('/api/config', headers={'X-Instance-ID': 'alpha'}).json['config']
                beta = client.get('/api/config', headers={'X-Instance-ID': 'beta'}).json['config']
                assert alpha['youtube_start_interval'] == 111 and len(alpha['youtube_cookie_pool']) == 2
                assert not beta['youtube_cookie_pool'] and beta['youtube_start_interval'] != 111
                page.screenshot(path=str(output / 'accounts-pool.png'), full_page=True)

                page.get_by_role('link', name='下载实例', exact=True).click()
                expect(page.get_by_role('heading', name='下载实例', exact=True)).to_be_visible()
                page.get_by_label('全局并发上限', exact=True).fill('7')
                page.get_by_role('button', name='保存全局上限', exact=True).click()
                expect(page.get_by_role('status')).to_contain_text('全局并发上限已保存')
                card = page.locator('section.card').filter(has=page.get_by_role('heading', name='beta', exact=True))
                card.get_by_text('实例详情与名称', exact=True).click()
                card.get_by_label('显示名称', exact=True).fill('Beta renamed')
                card.get_by_role('button', name='保存名称', exact=True).click()
                expect(page.get_by_role('heading', name='Beta renamed', exact=True)).to_be_visible()
                page.screenshot(path=str(output / 'instances.png'), full_page=True)

                proxy_states['beta']['testing'] = {'running': True, 'done': 1, 'total': 2}
                page.goto(base + '/proxy?instance_id=beta')
                expect(page.get_by_role('status')).to_contain_text('1 / 2')
                expect(page.get_by_role('button', name='全部测速', exact=True)).to_be_disabled()
                proxy_states['beta']['testing'] = {'running': False}
                page.reload()
                expect(page.get_by_role('button', name='全部测速', exact=True)).to_be_enabled()
                expect(page.get_by_role('cell', name='beta-one（当前）', exact=True)).to_be_visible()
                page.get_by_label('直连源地址', exact=True).fill('::')
                page.get_by_role('button', name='保存连接配置', exact=True).click()
                expect(page.get_by_role('status')).to_contain_text('连接配置已保存')
                assert client.get('/api/config', headers={'X-Instance-ID': 'alpha'}).json['config']['youtube_source_address'] == ''
                assert client.get('/api/config', headers={'X-Instance-ID': 'beta'}).json['config']['youtube_source_address'] == '::'
                page.get_by_label('订阅名称',exact=True).fill('Beta subscription')
                page.get_by_label('订阅链接',exact=True).fill('https://subscription.test/private-token')
                page.get_by_role('button',name='保存 / 更新订阅',exact=True).click()
                expect(page.get_by_role('status')).to_contain_text('订阅已保存')
                expect(page.get_by_label('订阅链接',exact=True)).to_have_value('')
                assert 'private-token' not in page.content()
                page.get_by_role('checkbox', name='开启自动轮换', exact=True).check()
                page.get_by_role('checkbox', name='beta-one', exact=True).check()
                page.get_by_role('checkbox', name='beta-two', exact=True).check()
                page.get_by_role('button', name='保存轮换配置', exact=True).click()
                expect(page.get_by_role('status')).to_contain_text('自动轮换配置已保存')
                page.get_by_role('button', name='测试 YouTube', exact=True).click()
                expect(page.get_by_role('status')).to_contain_text('12 ms · beta')
                page.screenshot(path=str(output / 'proxy.png'), full_page=True)

                page.goto(base + '/deployment?instance_id=beta')
                expect(page.get_by_label('数据库地址', exact=True)).to_have_value('beta.mysql.test')
                expect(page.get_by_label('新密码', exact=True)).to_have_value('')
                assert 'mysql-fixture-secret' not in page.content() and 'redis-fixture-secret' not in page.content()
                page.get_by_label('实例显示名称', exact=True).fill('Beta deployment')
                page.get_by_role('button', name='保存部署配置', exact=True).click()
                expect(page.get_by_text('配置已保存，等待重启本机服务生效。', exact=True)).to_be_visible()
                assert gateways['beta'].profile['instance_name'] == 'Beta deployment'
                assert gateways['alpha'].profile['instance_name'] == 'alpha'
                assert gateways['beta'].profile['mysql_password'] == 'mysql-fixture-secret'
                page.screenshot(path=str(output / 'deployment.png'), full_page=True)

                page.goto(base + '/statistics')
                expect(page.get_by_role('heading', name='交付与账单', exact=True)).to_be_visible()
                page.get_by_label('账本范围', exact=True).select_option('sessions')
                page.get_by_role('button', name=re.compile('下载会话 #')).first.click()
                session = page.get_by_role('dialog', name='执行会话明细', exact=True)
                expect(session).to_contain_text('Fixture video')
                session.get_by_label('会话备注', exact=True).fill('Reviewed session')
                # Live session refreshes must preserve an unsaved label draft.
                with page.expect_response(lambda response: '/api/statistics/sessions/' in response.url and response.request.method == 'GET'):
                    pass
                expect(session.get_by_label('会话备注', exact=True)).to_have_value('Reviewed session')
                session.get_by_role('button', name='保存备注', exact=True).click()
                expect(session.get_by_role('button', name='保存备注', exact=True)).to_be_disabled()
                session.get_by_role('button', name='关闭对话框', exact=True).click()
                expect(page.get_by_role('button', name='Reviewed session', exact=True)).to_be_visible()
                page.screenshot(path=str(output / 'statistics.png'), full_page=True)

                # Login failures remain visible; the form can be retried without reload loops.
                anonymous = browser.new_page(viewport={'width': 390, 'height': 844})
                anonymous.on('pageerror', lambda error: errors.append(str(error)))
                anonymous.goto(base + '/login')
                expect(anonymous.get_by_label('用户名', exact=True)).to_be_visible()
                anonymous.screenshot(path=str(output / 'login-public-390.png'), full_page=True)
                anonymous.set_viewport_size({'width': 1440, 'height': 1000})
                anonymous.screenshot(path=str(output / 'login-public.png'), full_page=True)
                anonymous.set_viewport_size({'width': 390, 'height': 844})
                anonymous.get_by_label('用户名', exact=True).fill('admin')
                anonymous.get_by_label('密码', exact=True).fill('wrong-password')
                anonymous.get_by_role('button', name='登录', exact=True).click()
                expect(anonymous.get_by_role('alert')).to_contain_text('用户名或密码不正确')
                expect(anonymous.get_by_label('用户名', exact=True)).to_have_value('admin')
                anonymous.get_by_label('密码', exact=True).fill('fixture-admin-password')
                anonymous.get_by_role('button', name='登录', exact=True).click()
                expect(anonymous.get_by_role('heading', name='任务中心', exact=True)).to_be_visible()
                anonymous.close()

                setup = browser.new_page(viewport={'width': 390, 'height': 844})
                setup.on('pageerror', lambda error: errors.append(str(error)))
                setup.goto(recovery_url + '/setup')
                expect(setup.get_by_role('heading', name='本机部署与恢复', exact=True)).to_be_visible()
                setup.get_by_label('部署设置访问码', exact=True).fill(recovery.access_code)
                setup.get_by_role('button', name='进入配置', exact=True).click()
                expect(setup.get_by_label('实例显示名称', exact=True)).to_have_value('Recovery')
                assert not setup.get_by_label('数据库地址', exact=True).count()
                assert 'recovery-secret' not in setup.content()
                setup.get_by_role('button', name='测试连接', exact=True).click()
                expect(setup.get_by_role('status')).to_contain_text('MySQL 连接成功')
                setup.screenshot(path=str(output / 'setup-390.png'), full_page=True)
                setup.close()

                for path in ('accounts','instances','proxy','deployment','statistics','login'):
                    page.goto(base+'/'+path+'?instance_id=beta')
                    page.set_viewport_size({'width':390,'height':844})
                    expect(page.get_by_role('heading',level=1)).to_be_visible()
                    page.wait_for_load_state('networkidle')
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), f'{path}: mobile overflow'
                    page.screenshot(path=str(output/f'{path}-390.png'),full_page=True)

                global_requests = [r for r in requests if PREFIX in r.url or '/api/statistics' in r.url or '/api/instances' in r.url]
                assert global_requests and all('x-instance-id' not in r.headers for r in global_requests)
                assert not errors, errors
                browser.close()
        finally:
            stop.set()
            beat.join(timeout=3)
            for server in servers:
                server.shutdown()
            for app in apps.values():
                app.extensions['task_store'].db.engine.dispose()
        print('Vue management verification passed; screenshots:', output)


if __name__ == '__main__':
    main()
