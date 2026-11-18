"""Verify disk consequences on settings and notifications using an isolated server."""
import copy,json,re,sys,threading,time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from playwright.sync_api import expect,sync_playwright
from werkzeug.serving import make_server
from scripts.verify.support import QuietHandler
from tests.test_console_auth import ConsoleAuthTests
from webapp.browser import browser_options
from webapp.disk_budget import DiskBudget,GIB
from webapp.disk_diagnostics import explain


def main():
    fixture=ConsoleAuthTests();fixture.setUp();server=None
    try:
        fixture.setup_admin()
        saved=fixture.client.put('/api/config',json=dict(disk_budget_gb=100,disk_test_budget_gb=80,
            disk_min_free_gb=10,disk_resume_free_gb=15),headers=fixture.headers)
        assert saved.status_code==200,saved.json
        budget=DiskBudget(fixture.store.directory,dict(disk_budget_gb=100,disk_test_budget_gb=80,
            disk_min_free_gb=10,disk_resume_free_gb=15))
        def sample(kind):
            with budget.connect() as db:
                db.execute('DELETE FROM requirements')
                budget._set(db,'pressure',{})
                budget._set(db,'retention_health',dict(status='ok',sampled_at=time.time(),completed_count=0,failed_count=0))
                budget._set(db,'disk_alert',{})
                if kind=='quota':
                    db.execute('INSERT INTO requirements VALUES (?,?,?,?,?)',('fixture-video',budget._version(),2*GIB,0,0))
                    budget._set(db,'pressure',dict(task_id='fixture-video',category='test',kind='budget'))
                if kind=='retention':
                    budget._set(db,'retention_health',dict(status='ok',sampled_at=time.time(),completed_count=3,
                        completed_bytes=2*GIB,pending_processing_count=2,pending_processing_bytes=GIB,failed_count=0))
            value={**budget.snapshot(),'free_bytes':114*GIB,'total_bytes':200*GIB,'low_space':False}
            if kind=='quota':
                value.update(used_bytes=int(79.25*GIB),committed_bytes=int(79.5*GIB))
                value['categories']['test'].update(used_bytes=int(79.25*GIB),committed_bytes=int(79.5*GIB))
            elif kind=='space':value.update(free_bytes=12*GIB,low_space=True)
            elif kind=='unknown':value.update(used_bytes=20*GIB,committed_bytes=100*GIB)
            budget.publish(fixture.store,value)
            value['diagnostic']=explain(budget,value)
            return value
        current=sample('quota')
        server=make_server('127.0.0.1',0,fixture.app,threaded=True,request_handler=QuietHandler)
        threading.Thread(target=server.serve_forever,daemon=True).start()
        base=f'http://127.0.0.1:{server.server_port}'
        output=Path('.runtime/verify/disk-explanation');output.mkdir(parents=True,exist_ok=True)
        errors=[]
        with sync_playwright() as pw:
            browser=pw.chromium.launch(headless=True,**browser_options(pw.chromium))
            context=browser.new_context(viewport=dict(width=1440,height=1050))
            name=fixture.app.config['SESSION_COOKIE_NAME']
            context.add_cookies([dict(name=name,value=fixture.client.get_cookie(name).value,url=base)])
            page=context.new_page();page.on('pageerror',lambda error:errors.append(str(error)))
            page.route(re.compile(r'/api/disk(?:\?.*)?$'),lambda route:route.fulfill(json=current))
            page.goto(base+'/settings#maintenance')
            diagnosis=page.get_by_role('region',name='磁盘空间诊断')
            expect(diagnosis).to_contain_text('1 个任务等待空间')
            expect(diagnosis).to_contain_text('还差约 1.5 GiB')
            expect(diagnosis).to_contain_text('磁盘本身还有 114.0 GiB 可用')
            expect(diagnosis).to_contain_text('全盘已用 43%')
            expect(page.get_by_role('button',name='清理可回收文件',exact=True)).to_be_visible()
            expect(diagnosis).to_contain_text('每个视频的一次执行先共同预留 1 GiB')
            page.screenshot(path=str(output/'quota-desktop.png'),full_page=True)
            page.get_by_role('button',name='查看空间诊断',exact=False).click()
            drawer=page.get_by_role('dialog',name='通知中心')
            expect(drawer).to_contain_text('是否影响下载')
            expect(drawer).to_contain_text('还差约 1.5 GiB')
            page.set_viewport_size(dict(width=390,height=844))
            expect(drawer).to_be_visible()
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            page.screenshot(path=str(output/'quota-mobile-notice.png'),full_page=True)
            page.get_by_role('button',name='关闭通知中心',exact=True).click()
            current=sample('space')
            page.get_by_role('button',name='刷新空间',exact=True).click()
            expect(diagnosis).to_contain_text('暂缓启动新下载')
            expect(diagnosis).to_contain_text('还差约 3.0 GiB')
            expect(diagnosis).to_contain_text('需恢复到 15.0 GiB')
            current=sample('unknown');page.get_by_role('button',name='刷新空间',exact=True).click()
            expect(diagnosis).to_contain_text('下一次申请量尚未确定')
            current=sample('retention');page.get_by_role('button',name='刷新空间',exact=True).click()
            expect(diagnosis).to_contain_text('当前未因空间限制下载')
            expect(diagnosis).to_contain_text('无需为本次清理提醒增加空间')
            current['diagnostic']['sampled_at']=time.time()-100
            page.get_by_role('button',name='刷新空间',exact=True).click()
            expect(diagnosis).to_contain_text('当前状态待更新')
            current=sample('normal');page.get_by_role('button',name='刷新空间',exact=True).click()
            expect(diagnosis).to_contain_text('无需处理')
            page.set_viewport_size(dict(width=1440,height=1050))
            page.screenshot(path=str(output/'normal-desktop.png'),full_page=True)
            page.goto(base+'/jobs')
            page.get_by_role('button',name=re.compile('^通知')).click()
            drawer=page.get_by_role('dialog',name='通知中心')
            drawer.get_by_role('button',name=re.compile('^已解决')).click()
            expect(drawer).to_contain_text('告警记录')
            expect(drawer).to_contain_text('当前状态请查看“空间与日志”')
            assert not errors,errors
            browser.close()
        print(json.dumps(dict(scenarios=['quota_at_43_percent','space_hysteresis','unknown_demand',
            'retention','stale','normal','resolved_history'],desktop=True,mobile=True,javascript_errors=errors)))
    finally:
        if server:server.shutdown()
        fixture.store.db.engine.dispose();fixture.doCleanups()


if __name__=='__main__':main()
