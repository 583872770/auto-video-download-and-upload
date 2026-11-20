"""Exercise outside dismissal against real Vue dialogs and isolated application data."""
import json
import base64
import os
from pathlib import Path
import sys
import threading
from unittest.mock import patch
from flask import request, jsonify

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tests.test_samples import SampleTests
from tests.test_console_auth import ConsoleAuthTests
from webapp.browser import browser_options
from werkzeug.serving import make_server
from scripts.verify.support import QuietHandler
from playwright.sync_api import sync_playwright, expect

environment = patch.dict(os.environ, {'VIDEO_ACCOUNT_ENCRYPTION_KEY': base64.urlsafe_b64encode(b'd' * 32).decode()})
environment.start()
fixture = SampleTests();fixture.setUp();fixture.addCleanup(environment.stop);server = None
submit_started = threading.Event();submit_release = threading.Event();block_submit = threading.Event()
def slow_submit():
    if request.path == '/api/jobs' and request.method == 'POST' and block_submit.is_set():
        submit_started.set();submit_release.wait(15)
        return jsonify(error='Isolated slow submission'), 503
fixture.app.before_request_funcs.setdefault(None, []).append(slow_submit)
output = Path('.runtime/verify/dialog-dismiss');output.mkdir(parents=True, exist_ok=True)
try:
    fixture.storage = {'storage_mode': 'test'}
    job = fixture.create(['metadata']);fixture.execute()
    # Scheduler fixtures use placeholder encrypted accounts; prepare valid test
    # secrets so the real account page can enable its administrator controls.
    from sqlalchemy import select
    from webapp.database import youtube_accounts
    from webapp.account_secrets import AccountSecrets
    key = AccountSecrets()
    with fixture.store.connect() as db:
        for identity in db.execute(select(youtube_accounts.c.id)).scalars():
            db.execute(youtube_accounts.update().where(youtube_accounts.c.id == identity).values(
                key_id=key.key_id, secrets_ciphertext=key.encrypt(identity, {'password': '', 'extra': ''})))
    fixture.token = lambda client: ConsoleAuthTests.token(fixture, client)
    ConsoleAuthTests.setup_admin(fixture)
    server = make_server('127.0.0.1', 0, fixture.app, threaded=True, request_handler=QuietHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f'http://127.0.0.1:{server.server_port}'
    errors = [];native_dialogs = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, **browser_options(pw.chromium))
        context = browser.new_context(viewport=dict(width=1440, height=1000))
        cookie = fixture.app.config['SESSION_COOKIE_NAME']
        context.add_cookies([dict(name=cookie, value=fixture.client.get_cookie(cookie).value, url=base)])
        page = context.new_page();page.on('pageerror', lambda error: errors.append(str(error)))
        page.on('dialog', lambda dialog: (native_dialogs.append(dialog.type), dialog.dismiss()))
        page.goto(base + '/jobs?view=videos')
        page.locator('#header-task').click()
        picker = page.get_by_role('dialog', name='选择运行任务', exact=True)
        expect(picker).to_be_visible()
        picker.get_by_role('textbox').click()
        expect(picker).to_be_visible()
        page.locator('.header-state').click()
        expect(picker).to_have_count(0)
        page.get_by_role('button', name='查看结果 / 文件', exact=True).click()
        result = page.get_by_role('dialog', name='视频结果与文件', exact=True)
        expect(result).to_be_visible()
        box = result.bounding_box()
        page.mouse.click(box['x'] + 8, box['y'] + 8)
        expect(result).to_be_visible()
        page.mouse.move(box['x'] + 8, box['y'] + 8);page.mouse.down()
        page.mouse.move(4, 4);page.mouse.up()
        expect(result).to_be_visible()
        result.get_by_role('button', name='查看全文', exact=True).click()
        expect(page.locator('dialog[open]')).to_have_count(2)
        expect(page.locator('dialog[open]').last.locator('.metadata-details')).to_contain_text('视频 ID')
        page.mouse.click(4, 4)
        expect(page.locator('dialog[open]')).to_have_count(1)
        expect(result).to_be_visible()
        result.get_by_role('button', name='重新下载并留样', exact=True).click()
        expect(page.locator('.el-message-box')).to_be_visible()
        page.mouse.click(4, 4)
        expect(page.locator('.el-message-box')).not_to_be_visible()
        expect(result).to_be_visible()
        page.mouse.click(4, 4)
        expect(result).to_have_count(0)

        page.goto(base + '/jobs')
        page.get_by_role('button', name='新建任务', exact=True).click()
        editor = page.get_by_role('dialog', name='新建任务', exact=True)
        editor.locator('[name=name]').fill('Outside draft')
        page.mouse.click(4, 4)
        confirmation = page.locator('.el-message-box')
        expect(confirmation).to_contain_text('未保存')
        page.mouse.click(4, 4)
        expect(confirmation).not_to_be_visible()
        expect(editor.locator('[name=name]')).to_have_value('Outside draft')
        page.mouse.click(4, 4)
        confirmation.get_by_role('button', name='放弃并继续', exact=True).click()
        expect(editor).to_have_count(0)

        page.get_by_role('button', name='新建任务', exact=True).click()
        editor.locator('[name=customer]').fill('Dialog fixture')
        editor.locator('[name=name]').fill('Pending submission')
        editor.locator('[name=price]').fill('0.000001')
        block_submit.set()
        editor.locator('[type=submit]').click()
        assert submit_started.wait(5), 'Create did not reach the isolated server'
        page.mouse.click(4, 4)
        expect(editor).to_be_visible()
        expect(editor.get_by_role('button', name='关闭对话框')).to_be_disabled()
        submit_release.set();block_submit.clear()
        expect(editor.get_by_role('alert')).to_contain_text('Isolated slow submission')
        page.mouse.click(4, 4)
        confirmation.get_by_role('button', name='放弃并继续', exact=True).click()
        expect(editor).to_have_count(0)

        page.get_by_role('button', name='通知', exact=False).first.click()
        drawer = page.get_by_role('dialog', name='通知中心', exact=True)
        expect(drawer).to_be_visible()
        drawer.get_by_role('heading', name='通知中心', exact=True).click()
        expect(drawer).to_be_visible()
        page.get_by_role('heading', name='任务中心', exact=True).click()
        expect(drawer).to_have_count(0)

        # Former browser-native confirmations now share the same dismiss behavior.
        page.goto(base + '/accounts')
        page.get_by_role('button', name='新增账号', exact=True).click()
        account = page.get_by_role('dialog', name='新增账号', exact=True)
        account.get_by_label('邮箱账号', exact=True).fill('draft@example.com')
        page.mouse.click(4, 4)
        expect(confirmation).to_contain_text('未保存')
        page.mouse.click(4, 4)
        expect(confirmation).not_to_be_visible()
        expect(account.get_by_label('邮箱账号', exact=True)).to_have_value('draft@example.com')
        page.mouse.click(4, 4)
        confirmation.get_by_role('button', name='确认', exact=True).click()
        expect(account).to_have_count(0)

        for width in (1440, 390):
            page.set_viewport_size(dict(width=width, height=1000))
            page.goto(base + '/jobs?view=videos')
            page.get_by_role('button', name='查看结果 / 文件', exact=True).click()
            expect(result).to_be_visible()
            page.screenshot(path=str(output / f'preview-{width}.png'), animations='disabled')
            page.mouse.click(4, 4)
            expect(result).to_have_count(0)
        assert not errors, errors
        assert not native_dialogs, native_dialogs
        browser.close()
    print(json.dumps(dict(outside=True, padding_and_drag=True, nested=True,
        confirmations=True, dirty_draft=True, busy_submission=True, notification=True, mobile=True,
        header_popover=True, native_dialogs=native_dialogs, javascript_errors=errors)))
finally:
    submit_release.set()
    if server:
        server.shutdown();server.server_close()
    fixture.doCleanups()
