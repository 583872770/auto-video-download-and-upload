"""Vue execution pages: real isolated fleet controls, stale reads, media and logs."""
from pathlib import Path
import json
import re
import sys
import tempfile
import threading
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from PIL import Image
from flask import request
from playwright.sync_api import expect, sync_playwright
from werkzeug.serving import make_server
from app import create_app
from scripts.verify.verify_jobs import QuietHandler
from webapp.browser import browser_options
from webapp.database import instances
from webapp.fleet_routes import rpc as instance_rpc


def main():
    output = Path('.runtime/verify/frontend-execution')
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        database_url = 'sqlite:///' + (root / 'shared.db').as_posix()
        apps, stores, servers = [], [], []
        for identity, role in [('alpha', 'hybrid'), ('beta', 'worker')]:
            config = root / (identity + '.yaml')
            config.write_text(f'path: videos-{identity}\nproxy_mode: direct\nmin_free_gb: 0\ndownload_methods: [downmp3]\n', encoding='utf-8')
            app = create_app(config, root / identity, start_worker=False, role=role,
                             instance_id=identity, instance_name='Download worker with a long production name ' + identity,
                             instance_url='http://127.0.0.1:1', database_url=database_url)
            apps.append(app)
            stores.append(app.extensions['task_store'])
            stores[-1].fleet.control(identity, {'action': 'resume'})
            server = make_server('127.0.0.1', 0, app, threaded=True, request_handler=QuietHandler)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            servers.append(server)
            with stores[-1].connect() as db:
                db.execute(instances.update().where(instances.c.id == identity).values(url=f'http://127.0.0.1:{server.server_port}'))
        base = f'http://127.0.0.1:{servers[0].server_port}'
        status_gate, status_entered = threading.Event(), threading.Event()
        workbench_gate, workbench_entered = threading.Event(), threading.Event()
        status_gate.set(); workbench_gate.set()
        @apps[0].before_request
        def delayed_reads():
            if request.headers.get('X-Instance-ID') != 'alpha' or request.method != 'GET': return
            if request.path == '/api/status' and not status_gate.is_set():
                status_entered.set(); status_gate.wait(15)
            if request.path == '/api/jobs/workbench' and not workbench_gate.is_set():
                workbench_entered.set(); workbench_gate.wait(15)
        client = apps[0].test_client()
        csrf = re.search(r'name="csrf-token" content="([^"]+)"',client.get('/').text)[1]
        headers = {'X-CSRF-Token': csrf}
        jobs = []
        for name, urls in [('Alpha order','npNnbj46_ik\n2Gu7q-KUXCk'),('Beta order','WMu6ANTg8mk\naaaaaaaaaaa')]:
            response = client.post('/api/jobs',json={'customer':'Fixture','name':name,'urls':urls},headers=headers)
            assert response.status_code == 201, response.json
            jobs.append(response.json['id'])
        def checked_rpc(fleet, target, path, **kwargs):
            return {'ready':True} if path == '/api/fleet/preflight' else instance_rpc(fleet,target,path,**kwargs)
        errors, controls, global_reads = [], [], []
        try:
            with patch('webapp.fleet_routes.rpc',side_effect=checked_rpc), sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True,**browser_options(pw.chromium))
                page = browser.new_page(viewport={'width':1440,'height':1050},reduced_motion='reduce')
                page.on('pageerror',lambda error:errors.append(str(error)))
                page.on('request',lambda request: controls.append(request) if request.method=='POST' and '/api/jobs/workbench' in request.url else None)
                page.on('request',lambda request: global_reads.append(request) if request.method=='GET' and ('/api/jobs?' in request.url or '/api/tasks?' in request.url) else None)
                page.goto(base+'/workbench?job='+jobs[0]+'&instance_id=alpha')
                expect(page.locator('#download-job')).to_have_value(jobs[0])
                expect(page.locator('#download-queue-start')).to_be_enabled()
                assert not controls, 'Preview started execution'
                page.locator('#download-queue-start').click()
                expect(page.locator('#download-queue-start')).to_have_text('已启动')
                assert 'job=' not in page.url, 'A completed start kept a stale preview link'
                page.locator('#download-job').select_option(jobs[1])
                expect(page.locator('#download-queue-start')).to_be_enabled()
                assert len(controls)==1, 'Selecting another job started it'
                page.reload()
                expect(page.locator('#download-job')).to_have_value(jobs[0])
                response = client.post('/api/jobs/workbench',json={'action':'start','job_id':jobs[1]},headers={**headers,'X-Instance-ID':'beta'})
                assert response.status_code==200,response.json
                claimed = [store.claim(store.owner) for store in stores]
                assert all(claimed)
                for index,store in enumerate(stores):
                    store.update_progress(claimed[index]['id'],{'stage':'downloading','percent':25,'speed':1024},store.owner)
                    store.log_path(claimed[index]['id']).write_text(f'origin {store.fleet.id}\n<script>window.__unsafeLog=true</script>\n',encoding='utf-8')

                # Capacity polling may hang; workbench progress must continue independently.
                status_gate.clear()
                page.reload()
                expect(page.locator('#workbench-tasks')).to_contain_text('25.0%',timeout=12000)
                assert status_entered.wait(5)
                status_gate.set()

                # Old executor responses cannot repaint a newly selected instance.
                workbench_gate.clear()
                page.reload()
                expect(page.get_by_label('当前实例',exact=True)).to_have_value('alpha')
                assert workbench_entered.wait(5)
                page.get_by_label('当前实例',exact=True).select_option('beta')
                expect(page.locator('#download-job')).to_have_value(jobs[1])
                workbench_gate.set()
                expect(page.locator('#download-job')).to_have_value(jobs[1])
                page.locator('#download-job-pause').click()
                expect(page.locator('#download-queue-start')).to_be_enabled()
                assert stores[1].heartbeat(claimed[1]['id'],stores[1].owner), 'Pause revoked an active lease'
                assert stores[0].heartbeat(claimed[0]['id'],stores[0].owner), 'Pause affected another instance'
                assert controls[-1].headers.get('x-instance-id')=='beta'
                page.locator('#download-job-stop').click()
                expect(page.get_by_role('dialog')).to_contain_text('beta')
                before = len(controls)
                page.get_by_role('button',name='关闭对话框',exact=True).click()
                assert len(controls)==before
                page.locator('#download-job-stop').click()
                page.get_by_role('button',name='确认立即停止',exact=True).click()
                expect(page.get_by_role('dialog')).to_have_count(0)
                assert not stores[1].heartbeat(claimed[1]['id'],stores[1].owner)
                assert stores[0].heartbeat(claimed[0]['id'],stores[0].owner)
                # Stop revokes a lease and requeues; create a settled history record separately.
                assert stores[1].finish(claimed[1]['id'],error='fixture settled failure')

                # Global history keeps task origin when fetching logs, with incremental offsets.
                nav=page.get_by_role('navigation',name='主导航')
                page.evaluate('window.__spaToken=17')
                nav.get_by_role('link',name='执行日志',exact=True).click()
                expect(page.locator('#execution-records')).to_contain_text('beta')
                assert page.evaluate('window.__spaToken')==17
                page.locator('[data-task-log]').first.click()
                expect(page.locator('#log-content')).to_contain_text('origin beta')
                assert page.evaluate('window.__unsafeLog') is None
                with stores[1].log_path(claimed[1]['id']).open('a',encoding='utf-8') as stream:
                    stream.write('second segment\n')
                page.get_by_role('button',name='刷新日志',exact=True).click()
                expect(page.locator('#log-content')).to_contain_text('second segment')
                assert page.locator('#log-content').inner_text().count('origin beta')==1
                page.get_by_role('button',name='关闭对话框',exact=True).click()
                assert all('x-instance-id' not in item.headers for item in global_reads)

                # Real per-instance catalog and files exercise media paging, seeking and import.
                media_root=root/'videos-beta'
                media_root.mkdir()
                for index in range(25):
                    path=media_root/f'clip-{index:02d}.mp4'
                    path.write_bytes(b'fixture video bytes')
                    Image.new('RGB',(32,18),(30,80,150)).save(path.with_suffix('.png'))
                    apps[1].extensions['media_catalog'].index_file(path,media_root)
                page.goto(base+'/media?instance_id=beta')
                expect(page.locator('[data-media]')).to_have_count(24)
                page.screenshot(path=str(output/'media-populated.png'),full_page=True,animations='disabled')
                page.locator('#media-next').click()
                expect(page.locator('[data-media]')).to_have_count(1)
                page.locator('#media-prev').click()
                expect(page.locator('[data-media]')).to_have_count(24)
                page.locator('[data-media]').first.click()
                player=page.locator('dialog video')
                expect(player).to_have_attribute('src',re.compile('instance_id=beta'))
                result=page.request.get(base+player.get_attribute('src'),headers={'Range':'bytes=0-4'})
                assert result.status==206 and len(result.body())==5
                page.get_by_role('button',name='关闭对话框',exact=True).click()
                expect(page.locator('video')).to_have_count(0)
                page.get_by_role('button',name='导入视频',exact=True).click()
                page.locator('dialog input[type=file]').nth(0).set_input_files({'name':'imported.mp4','mimeType':'video/mp4','buffer':b'isolated import'})
                page.get_by_role('button',name='确认导入',exact=True).click()
                expect(page.locator('dialog [role=alert]')).to_contain_text('对应封面')
                page.locator('dialog input[type=file]').nth(1).set_input_files(str(media_root/'clip-00.png'))
                # The fixture still has queued downloads: backend must retain its conflict rule.
                page.get_by_role('button',name='确认导入',exact=True).click()
                expect(page.locator('dialog [role=alert]')).to_contain_text('等待当前任务结束')
                page.get_by_role('button',name='关闭对话框',exact=True).click()
                page.get_by_role('button',name='放弃并继续',exact=True).click()
                expect(page.get_by_role('dialog')).to_have_count(0)
                # Complete the isolated fixture queue, then exercise a successful multipart import.
                for pending in stores[0].page(status='running',limit=100)['items']:
                    stores[0].finish(pending['id'],error='fixture end')
                page.get_by_role('button',name='导入视频',exact=True).click()
                page.locator('dialog input[type=file]').nth(0).set_input_files({'name':'imported.mp4','mimeType':'video/mp4','buffer':b'isolated import'})
                page.locator('dialog input[type=file]').nth(1).set_input_files(str(media_root/'clip-00.png'))
                page.get_by_role('button',name='确认导入',exact=True).click()
                expect(page.get_by_role('dialog')).to_have_count(0)
                assert (media_root/'imported.mp4').read_bytes()==b'isolated import'

                page.get_by_role('button',name='清空素材库',exact=True).click()
                page.get_by_role('button',name='确认清空',exact=True).click()
                expect(page.locator('dialog [role=alert]')).to_contain_text('输入“清空”')
                page.get_by_role('button',name='关闭对话框',exact=True).click()
                assert all((media_root/f'clip-{index:02d}.mp4').is_file() for index in range(25))
                alpha_root=root/'videos-alpha'
                alpha_root.mkdir()
                sentinel=alpha_root/'keep.mp4'
                sentinel.write_bytes(b'other executor')
                page.get_by_role('button',name='清空素材库',exact=True).click()
                page.get_by_label('确认清空素材',exact=True).fill('清空')
                page.get_by_role('button',name='确认清空',exact=True).click()
                expect(page.locator('dialog [role=alert]')).to_contain_text('暂停全部下载实例')
                stores[0].fleet.control('alpha',{'action':'drain'})
                page.get_by_role('button',name='确认清空',exact=True).click()
                expect(page.locator('#log-content')).to_be_visible()
                page.get_by_role('button',name='刷新日志',exact=True).click()
                expect(page.get_by_role('dialog')).to_contain_text('素材库清理完成',timeout=15000)
                page.get_by_role('button',name='关闭对话框',exact=True).click()
                page.get_by_role('button',name='查询',exact=True).click()
                expect(page.locator('[data-media]')).to_have_count(0,timeout=10000)
                assert sentinel.read_bytes()==b'other executor' and not (media_root/'imported.mp4').exists()
                for path in ('workbench','media','logs'):
                    page.goto(base+'/'+path+'?instance_id=beta')
                    expect(page.get_by_role('heading',level=1)).to_be_visible()
                    if path=='workbench':
                        expect(page.locator('#download-job-note')).to_have_text('Fixture / Beta order')
                        expect(page.get_by_role('heading',name='执行容量与等待原因',exact=True)).to_be_visible()
                    elif path=='media': expect(page.locator('main')).to_contain_text('暂无符合条件的素材')
                    else: expect(page.locator('#execution-records')).to_contain_text('beta')
                    for width in (1440,768,390):
                        page.set_viewport_size({'width':width,'height':1050})
                        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'),f'{path}: overflow {width}'
                        page.screenshot(path=str(output/f'{path}-{width}.png'),full_page=True,animations='disabled')
                assert not errors,errors
                browser.close()
            print(json.dumps({'ok':True,'browser_errors':errors,'screenshots':str(output)},ensure_ascii=False))
        finally:
            status_gate.set();workbench_gate.set()
            for server in servers:
                server.shutdown();server.server_close()


if __name__=='__main__':
    main()
