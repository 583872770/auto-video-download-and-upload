"""Exercise the production account workspace against isolated dummy data."""
import base64
import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from playwright.sync_api import sync_playwright, expect
from werkzeug.serving import make_server
from app import create_app


def main():
    environment={k:v for k,v in os.environ.items() if not k.startswith(('VIDEO_','MYSQL_','YT5S_'))}
    environment['VIDEO_ACCOUNT_ENCRYPTION_KEY']=base64.urlsafe_b64encode(b'u' * 32).decode()
    output=Path('.runtime/cache-archive-20261111/account-library-ui');output.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ,environment,clear=True), patch('webapp.youtube_auth.probe',return_value=('valid','Fixture login confirmed')) as probe:
        root=Path(temporary);config=root/'config.yaml'
        config.write_text('path: videos\nproxy_mode: direct\n',encoding='utf-8')
        app=create_app(config,root/'state',start_worker=False)
        login_ready,login_complete=threading.Event(),threading.Event()
        login_options=[]
        async def fixture_login(config,output,*,on_status,cancelled,**options):
            from scripts.login_youtube import LoginCancelled
            login_options.append(options)
            on_status('waiting','Fixture: finish verification in browser')
            login_ready.set()
            while not login_complete.is_set():
                if cancelled.is_set():raise LoginCancelled('Fixture cancelled')
                await asyncio.sleep(0.01)
            output.write_bytes(b'# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tSID\tlogin-ui-dummy-cookie\n')
            return {'account_name':options['expected_email']}
        login_patch=patch('webapp.youtube_login.login',fixture_login)
        login_patch.start()
        server=make_server('127.0.0.1',0,app,threaded=True)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        errors=[]
        try:
            with sync_playwright() as pw:
                browser=pw.chromium.launch(executable_path='C:/Program Files/Google/Chrome/Application/chrome.exe',headless=True)
                page=browser.new_page(viewport={'width':1440,'height':1100})
                page.on('pageerror',lambda error: errors.append(str(error)))
                page.goto(f'http://127.0.0.1:{server.server_port}/#accounts')
                # Bootstrap a real admin session through the CSRF-protected API.
                status=page.evaluate('''async key => {
                  const csrf=document.querySelector('meta[name="csrf-token"]').content;
                  const r=await fetch('/api/auth/setup',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrf},body:JSON.stringify({bootstrap_key:key,username:'admin',password:'ui-test-only-password'})}); return r.status;
                }''',app.extensions['console_auth'].bootstrap_key)
                assert status==201,status
                page.reload()
                expect(page.locator('#account-new')).to_be_enabled()
                page.locator('#account-new').click()
                form=page.locator('#account-edit-form')
                form.locator('[name=email]').fill('demo-one@example.com')
                form.locator('[name=password]').fill('dummy-!&#-password')
                form.locator('[name=instance_id]').select_option(index=1)
                form.locator('[name=note]').fill('UI fixture')
                form.locator('[type=submit]').click()
                expect(page.locator('#account-detail-title')).to_have_text('demo-one@example.com')
                page.locator('[data-account-secret=password]').click()
                expect(page.locator('#account-secret-value')).to_have_value('dummy-!&#-password')
                page.locator('#account-detail-dialog [data-close]').click()
                expect(page.locator('#account-secret-value')).to_have_value('')
                expect(page.locator('#account-rows')).to_contain_text('demo-one@example.com')
                assert not probe.called
                page.locator('#account-import').click()
                page.locator('#account-import-text').fill('demo-two@example.com----dummy2----opaque----http://example.com/\ndemo-three@example.com----dummy3')
                page.locator('#account-import-instance').select_option(index=1)
                page.locator('#account-import-form [type=submit]').click()
                expect(page.locator('#account-import-confirm')).to_be_enabled()
                assert 'dummy2' not in page.locator('#account-import-preview').inner_text()
                page.locator('#account-import-confirm').click()
                expect(page.locator('#account-import-dialog')).not_to_be_visible()
                expect(page.locator('#account-rows tr')).to_have_count(3)
                page.screenshot(path=str(output/'accounts-desktop.png'),full_page=True)
                page.locator('[data-account-tab=cookies]').click()
                page.locator('#credential-upload').click()
                expect(page.locator('#account-upload-dialog')).to_be_visible()
                page.locator('#account-upload-file').set_input_files({'name':'demo-cookie.txt','mimeType':'text/plain','buffer':b'# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tSID\tui-dummy-cookie\n'})
                page.locator('#account-upload-confirmed').check()
                page.locator('#account-upload-form [type=submit]').click()
                try:
                    expect(page.locator('#account-upload-dialog')).not_to_be_visible()
                except AssertionError:
                    print(json.dumps({'page_errors':errors,'validity':page.locator('#account-upload-form').evaluate('(f)=>[...f.elements].map(n=>({id:n.id,valid:n.checkValidity(),message:n.validationMessage,value:n.type==="file"?n.files.length:n.value}))'),'toasts':page.locator('#toasts').inner_text()},ensure_ascii=False))
                    raise
                expect(page.locator('#credential-rows tr')).to_have_count(1)
                assert not probe.called
                page.locator('#credential-rows [data-credential-check]').click()
                expect(page.locator('#credential-rows')).to_contain_text('最近验证通过')
                assert probe.call_count==1
                # Workbench/detail links also resolve the standalone executor,
                # whose selected-instance id is empty.
                profile=page.request.get(f'http://127.0.0.1:{server.server_port}/api/account-library/youtube/cookies').json()['items'][0]['id']
                page.evaluate('(id)=>openAccountCookie("",id,"schedule")',profile)
                expect(page.locator('#account-detail-title')).to_have_text('demo-one@example.com')
                expect(page.locator('#account-events')).to_contain_text('最近异常与间隔调整')
                page.locator('#account-detail-dialog [data-close]').click()
                page.locator('[data-account-tab=pool]').click()
                expect(page.locator('#youtube-cookie-list [data-cookie-pool-id]')).to_have_count(1)
                policy=page.locator('.settings-policy-details');policy.locator('summary').click()
                page.locator('[name=youtube_start_interval]').fill('90')
                page.locator('#account-pool-form [type=submit]').click()
                expect(page.locator('#account-pool-save-state')).to_contain_text('已保存')
                assert page.evaluate('state.config.youtube_start_interval')==90
                assert page.evaluate('settingsDirty') is False
                # All configuration controls have exactly one editable copy.
                assert page.evaluate('''() => [...document.querySelectorAll('#settings-form [name],#account-pool-form [name],#youtube-source-form [name]')].map(n=>n.name).every((n,i,a)=>a.indexOf(n)===i)''')
                page.locator('[data-account-tab=library]').click()
                page.locator('#account-rows [data-account-open]').first.click()
                page.locator('#account-edit-form [name=note]').fill('unsaved note')
                page.keyboard.press('Escape')
                expect(page.locator('dialog[open]')).to_have_count(2)
                page.get_by_role('button',name='取消',exact=True).last.click()
                expect(page.locator('#account-detail-dialog')).to_be_visible()
                page.locator('#account-detail-dialog [data-close]').click()
                page.get_by_role('button',name='确认',exact=True).last.click()
                expect(page.locator('#account-detail-dialog')).not_to_be_visible()
                # Account login has its own target and keeps saved passwords off the UI.
                row=page.locator('#account-rows tr').filter(has_text='demo-one@example.com')
                expect(row.locator('[data-account-login]')).to_be_enabled()
                row.locator('[data-account-login]').click()
                expect(page.locator('#account-login-dialog')).to_be_visible()
                expect(page.locator('#account-login-automatic')).to_be_checked()
                expect(page.locator('#account-login-instance')).to_be_disabled()
                expect(page.locator('#account-login-start')).to_be_enabled()
                page.locator('#account-login-start').click()
                assert login_ready.wait(5)
                expect(page.locator('#account-login-cancel')).to_be_visible()
                assert login_options[0]['credentials']['password']=='dummy-!&#-password'
                assert 'dummy-!&#-password' not in page.locator('#account-login-dialog').inner_text()
                login_complete.set()
                expect(page.locator('#account-login-status')).to_contain_text('已新增 Cookie 并关联此账号',timeout=10000)
                page.screenshot(path=str(output/'account-login.png'),full_page=True)
                page.locator('#account-login-dialog [data-close]').click()
                expect(page.locator('#account-login-dialog')).not_to_be_visible()
                linked=page.request.get(f'http://127.0.0.1:{server.server_port}/api/account-library/youtube?q=demo-one').json()['items'][0]
                assert linked['cookie_count']==2
                assert all(not c['pooled'] for c in linked['cookies'])
                page.set_viewport_size({'width':390,'height':844})
                page.screenshot(path=str(output/'accounts-mobile.png'),full_page=True,animations='disabled')
                assert page.locator('#sidebar').bounding_box()['x'] + page.locator('#sidebar').bounding_box()['width'] <= 1
                assert page.evaluate('document.documentElement.scrollWidth<=window.innerWidth'), 'mobile page overflow'
                page.emulate_media(color_scheme='dark')
                page.screenshot(path=str(output/'accounts-dark.png'),full_page=True)
                assert not errors, errors
                browser.close()
        finally:
            app.extensions['youtube_login'].cancel()
            if app.extensions['youtube_login'].thread:app.extensions['youtube_login'].thread.join(5)
            login_patch.stop()
            server.shutdown();thread.join(timeout=5)
    print(json.dumps({'passed':True,'screenshots':str(output),'page_errors':errors}))


if __name__=='__main__': main()
