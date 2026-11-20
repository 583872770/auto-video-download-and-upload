"""Exercise failed-test remedies against an isolated backend and real media fixtures."""
import json
from pathlib import Path
import sys
import threading

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from playwright.sync_api import expect, sync_playwright
from werkzeug.serving import make_server
from scripts.verify.support import QuietHandler
from tests.test_retention import FailedTestResolutionTests
from tests.test_console_auth import ConsoleAuthTests
from webapp.browser import browser_options


def main():
    fixture=FailedTestResolutionTests();fixture.setUp();server=None
    try:
        entries=[fixture.make_failed() for _ in range(3)]
        from webapp.database import tasks
        from webapp.disk_budget import DiskBudget
        with fixture.store.connect() as db:
            db.execute(tasks.update().values(finished_at='2020-01-01T00:00:00+00:00'))
        budget=DiskBudget(fixture.store.directory,fixture.store.runtime())
        budget._retention_health(fixture.store,0)
        budget.publish(fixture.store,budget.snapshot(force=True))
        fixture.token=lambda client:ConsoleAuthTests.token(fixture,client)
        ConsoleAuthTests.setup_admin(fixture)
        server=make_server('127.0.0.1',0,fixture.app,threaded=True,request_handler=QuietHandler)
        threading.Thread(target=server.serve_forever,daemon=True).start()
        base=f'http://127.0.0.1:{server.server_port}'
        output=Path('.runtime/verify/failed-tests');output.mkdir(parents=True,exist_ok=True)
        errors=[]
        with sync_playwright() as pw:
            browser=pw.chromium.launch(headless=True,**browser_options(pw.chromium))
            context=browser.new_context(viewport=dict(width=1440,height=1080))
            name=fixture.app.config['SESSION_COOKIE_NAME']
            context.add_cookies([dict(name=name,value=fixture.client.get_cookie(name).value,url=base)])
            page=context.new_page();page.on('pageerror',lambda error:errors.append(str(error)))
            page.goto(base+'/settings?instance_id=local#maintenance')
            section=page.get_by_role('region',name='失败测试处理')
            expect(section.locator('article')).to_have_count(3)
            expect(section).to_contain_text('这些测试已停止，不会自动继续')
            page.get_by_role('button',name='查看空间诊断').click()
            drawer=page.get_by_role('dialog',name='通知中心')
            drawer.get_by_role('link',name='前往空间与日志处理').first.click()
            expect(drawer).to_have_count(0)
            expect(section.locator('article')).to_have_count(3)
            section.screenshot(path=str(output/'desktop.png'))
            page.set_viewport_size(dict(width=390,height=844))
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            section.screenshot(path=str(output/'mobile.png'))
            for action,expected_count in [('放弃并清理',2),('重试本地处理',1),('重新下载',0)]:
                section.get_by_role('button',name=action,exact=True).first.click()
                dialog=page.locator('.el-message-box')
                expect(dialog).to_contain_text('npNnbj46_ik')
                expect(dialog).to_contain_text('已登记统计保留')
                if action=='放弃并清理':
                    dialog.get_by_role('button',name='取消',exact=True).click()
                    expect(section.locator('article')).to_have_count(3)
                    section.get_by_role('button',name=action,exact=True).first.click()
                dialog.get_by_role('button',name=action,exact=True).click()
                expect(section.locator('article')).to_have_count(expected_count)
            expect(section).to_contain_text('没有待处理的失败测试文件')
            assert not errors,errors
            browser.close()
        print(json.dumps(dict(desktop=True,mobile=True,notification_link='passed',actions=['cancel','discard','retry','redownload'],javascript_errors=errors)))
    finally:
        if server:server.shutdown()
        fixture.store.db.engine.dispose();fixture.doCleanups()


if __name__=='__main__':main()
