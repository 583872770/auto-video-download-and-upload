"""Exercise visual setup with an isolated database and no external services."""
import os
from pathlib import Path
import sys
import tempfile
import threading
import uuid
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from playwright.sync_api import expect, sync_playwright
from flask import Flask
from werkzeug.serving import make_server

from app import create_app
from scripts.verify_web import QuietHandler
from Utils.runtime import ROOT
from webapp.browser import browser_options
from webapp.deployment import DeploymentGateway, write_profile
from webapp.database import SchemaUpgradeInProgress


def verify_slow_start(page, root, config, output, *, failure=False):
    entered, release = threading.Event(), threading.Event()
    directory = root / ('failed-state' if failure else 'slow-state')
    directory.mkdir()
    write_profile(directory / 'deployment.json', {
        'role': 'manager', 'instance_id': 'slow-manager', 'instance_name': '慢速初始化测试',
        'instance_url': '', 'mysql_host': 'fixture.example.test', 'mysql_port': 3306,
        'mysql_database': 'fixture', 'mysql_user': 'fixture', 'mysql_password': 'fixture-password',
        'web_hosts': 'localhost,127.0.0.1'})

    fail_once = [failure]
    def factory(*args, **kwargs):
        entered.set()
        if not release.wait(20):
            raise RuntimeError('fixture timeout')
        if fail_once and fail_once.pop():
            raise SchemaUpgradeInProgress()
        app = Flask('slow-start-fixture')
        app.add_url_rule('/', view_func=lambda: 'Ready')
        return app

    gateway = DeploymentGateway(config, directory, load_environment=False, app_factory=factory)
    server = make_server('127.0.0.1', 0, gateway, threaded=True, request_handler=QuietHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    gateway.start()
    try:
        assert entered.wait(3)
        page.goto(f'http://127.0.0.1:{server.server_port}/setup', wait_until='networkidle')
        page.locator('#access-code').fill(gateway.access_code)
        page.locator('#unlock-form button').click()
        expect(page.locator('#deployment-state')).to_contain_text('正在连接 MySQL')
        expect(page.locator('#save-deployment')).to_be_disabled()
        page.locator('[name=instance_name]').fill('保留页面编辑')
        page.screenshot(path=str(output / 'setup-starting.png'), full_page=True)
        release.set()
        if failure:
            expect(page.locator('#deployment-error-title')).to_have_text('数据库仍在升级或等待表结构锁。')
            expect(page.locator('#deployment-error-remedy')).to_contain_text('不要反复提交')
            page.locator('#deployment-error summary').click()
            expect(page.locator('#deployment-error-details')).to_contain_text('SchemaUpgradeInProgress')
            page.screenshot(path=str(output / 'setup-upgrade-error.png'), full_page=True)
            page.set_viewport_size({'width': 390, 'height': 844})
            assert page.evaluate('() => document.documentElement.scrollWidth <= innerWidth')
            page.screenshot(path=str(output / 'setup-upgrade-error-mobile.png'), full_page=True)
            page.set_viewport_size({'width': 1440, 'height': 1080})
            page.locator('#save-deployment').click()
            expect(page.locator('#setup-success')).to_be_visible(timeout=10000)
            expect(page.locator('#deployment-error')).to_be_hidden()
            return
        expect(page.locator('#success-message')).to_have_text('服务初始化完成。', timeout=10000)
        expect(page.locator('#save-deployment')).to_be_enabled()
        expect(page.locator('[name=instance_name]')).to_have_value('保留页面编辑')
        expect(page.locator('#enter-workbench')).to_be_visible()
    finally:
        release.set()
        gateway.initialization.join(timeout=5)
        server.shutdown()
        thread.join(timeout=5)


def verify_role_recovery(page, root, config, output):
    directory = root / 'cluster-recovery-state'
    directory.mkdir()
    write_profile(directory / 'fleet-runtime.json', {'role': 'worker', 'id': 'original-download-node', 'boot_id': 'fixture-boot'})
    write_profile(directory / 'deployment.json', {
        'role': 'all', 'instance_id': '', 'instance_name': '原下载实例', 'instance_url': '',
        'mysql_host': 'fixture.example.test', 'mysql_port': 3306, 'mysql_database': 'fixture',
        'mysql_user': 'fixture', 'mysql_password': 'fixture-password', 'web_hosts': 'localhost,127.0.0.1'})
    calls = []

    def factory(*args, **kwargs):
        calls.append(kwargs)
        return Flask('recovered-worker')

    gateway = DeploymentGateway(config, directory, load_environment=False, app_factory=factory)
    server = make_server('127.0.0.1', 0, gateway, threaded=True, request_handler=QuietHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        page.goto(f'http://127.0.0.1:{server.server_port}/setup', wait_until='networkidle')
        page.locator('#access-code').fill(gateway.access_code)
        page.locator('#unlock-form button').click()
        expect(page.locator('#cluster-state-note')).to_contain_text('下载实例')
        expect(page.locator('#cluster-state-note')).to_contain_text('original-download-node')
        expect(page.locator('#deployment-role')).to_have_value('worker')
        expect(page.locator('#deployment-role option[value="all"]')).not_to_have_attribute('disabled', '')
        expect(page.locator('[name=instance_id]')).to_have_value('original-download-node')
        page.locator('#deployment-role').select_option('manager')
        page.locator('[name=instance_id]').fill('edited-node')
        page.locator('#restore-instance').click()
        expect(page.locator('#deployment-role')).to_have_value('worker')
        expect(page.locator('[name=instance_id]')).to_have_value('original-download-node')
        expect(page.locator('#instance-url-field')).to_be_visible()
        assert not calls
        page.screenshot(path=str(output / 'setup-role-recovery.png'), full_page=True)
        page.set_viewport_size({'width': 390, 'height': 844})
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        page.screenshot(path=str(output / 'setup-role-recovery-mobile.png'), full_page=True)
        page.locator('#save-deployment').click()
        expect(page.locator('#setup-success')).to_be_visible(timeout=10000)
        expect(page.locator('#deployment-error')).to_be_hidden()
        assert calls[0]['role'] == 'worker'
        assert calls[0]['instance_id'] == 'original-download-node'
        page.set_viewport_size({'width': 1440, 'height': 1080})
    finally:
        if gateway.initialization:
            gateway.initialization.join(timeout=5)
        server.shutdown()
        thread.join(timeout=5)


def main():
    output = ROOT / '.cache/validation' / ('deployment-' + uuid.uuid4().hex[:10])
    output.mkdir(parents=True)
    errors = []
    apps = []
    # Keep OS paths needed by Chromium, but suppress all application credentials.
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith(('VIDEO_', 'MYSQL_', 'YT5S_'))}
    with tempfile.TemporaryDirectory(prefix='frameflow-setup-ui-') as temporary, \
            patch.dict(os.environ, environment, clear=True), patch('webapp.deployment.test_mysql'):
        root = Path(temporary)
        config = root / 'config.yaml'
        config.write_text('path: videos\nproxy_mode: direct\nmin_free_gb: 0\ndownload_method: downmp3\n', encoding='utf-8')

        def factory(config_path, state_dir, **kwargs):
            kwargs['database_url'] = 'sqlite:///' + (root / 'fixture.sqlite3').as_posix()
            app = create_app(config_path, state_dir, start_worker=False, **kwargs)
            apps.append(app)
            return app

        gateway = DeploymentGateway(config, root / 'state', load_environment=False, app_factory=factory)
        server = make_server('127.0.0.1', 0, gateway, threaded=True, request_handler=QuietHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        address = f'http://127.0.0.1:{server.server_port}'
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True, **browser_options(pw.chromium))
                try:
                    page = browser.new_page(viewport={'width': 1440, 'height': 1080}, reduced_motion='reduce')
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    page.goto(address, wait_until='networkidle')
                    expect(page).to_have_url(address + '/setup')
                    expect(page.locator('#unlock-panel')).to_be_visible()
                    page.locator('#access-code').fill(gateway.access_code)
                    page.locator('#unlock-form button').click()
                    expect(page.locator('#deployment-form')).to_be_visible()
                    page.locator('[name=mysql_host]').fill('fixture.example.test')
                    page.locator('[name=mysql_user]').fill('video')
                    page.locator('[name=mysql_password]').fill('fixture-private-password')
                    expect(page.locator('#deployment-role')).to_have_value('hybrid')
                    expect(page.locator('#instance-url-field')).to_be_visible()
                    expect(page.locator('[name=instance_url]')).to_have_value(address)
                    page.locator('[name=instance_name]').fill('管理与下载一体实例')
                    page.locator('#test-database').click()
                    expect(page.locator('#setup-feedback')).to_have_text('MySQL 连接成功。')
                    page.screenshot(path=str(output / 'setup-desktop.png'), full_page=True)
                    page.set_viewport_size({'width': 390, 'height': 844})
                    assert page.evaluate('() => document.documentElement.scrollWidth <= innerWidth')
                    page.screenshot(path=str(output / 'setup-mobile.png'), full_page=True)
                    page.locator('#save-deployment').click()
                    expect(page.locator('#setup-success')).to_be_visible(timeout=10000)
                    expect(page.locator('#success-message')).to_contain_text('服务')
                    expect(page.locator('#mysql-password')).to_have_value('')
                    assert gateway.path.is_file()
                    assert len(apps) == 1
                    page.set_viewport_size({'width': 1440, 'height': 1080})
                    page.locator('#enter-workbench').click()
                    expect(page.locator('#fleet-selection')).to_have_value(gateway.profile['instance_id'])
                    expect(page.locator('.nav-item[href="/setup"]')).to_be_visible()
                    page.locator('.nav-item[href="/setup"]').click()
                    expect(page.locator('#deployment-form')).to_be_visible()
                    expect(page.locator('#mysql-password')).to_have_value('')
                    page.locator('#deployment-role').select_option('worker')
                    expect(page.locator('#instance-url-field')).to_be_visible()
                    page.locator('[name=instance_url]').fill('http://10.0.0.12:5000')
                    page.locator('#save-deployment').click()
                    expect(page.locator('#success-message')).to_have_text('配置已保存，重启本机服务后生效。')
                    expect(page.locator('#back-link')).to_be_visible()
                    assert gateway.running_role == 'hybrid'
                    assert len(apps) == 1
                    page.screenshot(path=str(output / 'setup-restart.png'), full_page=True)
                    page.locator('#deployment-role').select_option('all')
                    expect(page.locator('#single-transition-field')).to_be_visible()
                    expect(page.locator('#single-transition-confirm')).not_to_be_checked()
                    page.locator('#single-transition-confirm').check()
                    page.locator('#save-deployment').click()
                    expect(page.locator('#success-message')).to_contain_text('单机转换配置已保存')
                    expect(page.locator('#deployment-role')).to_have_value('all')
                    expect(page.locator('#single-transition-field')).to_be_hidden()
                    assert gateway.running_role == 'hybrid'
                    page.screenshot(path=str(output / 'setup-single-pending.png'), full_page=True)
                    restarted = DeploymentGateway(config, root / 'state', load_environment=False, app_factory=factory)
                    restarted.start()
                    restarted.initialization.join(timeout=10)
                    assert restarted.running_role == 'all', restarted.startup_error
                    assert restarted.active.extensions['task_store'].fleet is None
                    assert restarted.active.extensions['task_store'].downloads_paused()
                    verify_slow_start(page, root, config, output)
                    verify_slow_start(page, root, config, output, failure=True)
                    verify_role_recovery(page, root, config, output)
                    assert not errors, errors
                finally:
                    browser.close()
        finally:
            server.shutdown()
            thread.join(timeout=5)
            for app in apps:
                app.extensions['task_store'].db.engine.dispose()
    print(f'Visual setup verified; screenshots: {output}')


if __name__ == '__main__':
    main()
