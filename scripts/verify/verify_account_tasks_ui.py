"""Compact Vue account rows: real task grouping, counted filters, paging and stale samples."""
import hashlib
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
from tests.account_cookie_fixture import AccountCookieProfiles
from webapp.browser import browser_options
from webapp.cookie_pool import CookiePool
from webapp.database import cookie_profiles, download_permits, tasks
from webapp.settings import save_settings
from webapp.throughput import _atomic, policy_key


class QuietHandler(WSGIRequestHandler):
    def log(self, *args, **kwargs):
        pass


def main():
    output = Path('.runtime/verify/account-tasks')
    output.mkdir(parents=True, exist_ok=True)
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith(('VIDEO_', 'MYSQL_', 'YT5S_'))}
    with tempfile.TemporaryDirectory(prefix='account-tasks-') as temporary, patch.dict(os.environ, environment, clear=True):
        root = Path(temporary)
        config_path = root / 'config.yaml'
        config_path.write_text('path: videos\nproxy_mode: direct\nworker_count: 64\nmin_free_gb: 0\n'
                               'builtin_transfer_limit: 12\nbuiltin_connection_limit: 64\n'
                               'download_connection_limit: 64\n', encoding='utf-8')
        app = create_app(config_path, root / 'state', start_worker=False, role='hybrid',
                         instance_id='preview', database_url='sqlite:///' + (root / 'fixture.db').as_posix())
        store = app.extensions['task_store']
        library = AccountCookieProfiles(root, store)
        data = b'# HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tSID\tFIXTURE\n'
        ids = [library.add(f'account{i + 1:02d}@example.test', data.replace(b'FIXTURE', str(i).encode()))['id']
               for i in range(19)]
        with store.connect() as db:
            db.execute(cookie_profiles.update().where(cookie_profiles.c.id == ids[6]).values(content='invalid fixture'))
        local = store.fleet.local_settings()
        config = {**local['config'], 'youtube_cookie_pool': ids, 'youtube_cookie_session_limit': 3,
                  'youtube_cookie': library.require(ids[0]).relative_to(root).as_posix()}
        save_settings(config_path, config)
        store.fleet.save_local(config, local['base'], local['clash_directory'])
        store.fleet.control('preview', {'action': 'resume'})
        pool = CookiePool(store, config, root, None, strict=False)

        def seed(db, stamp):
            for identity, _, scope in pool.accounts:
                state = pool.router._state(db, scope)
                state.update(limit=3, interval=5)
                if identity == ids[3]:
                    state['next_at'] = stamp + 120
                if identity == ids[4]:
                    state.update(cooldown_until=stamp + 600, cooldown_kind='repeated_http_403')
                if identity == ids[5]:
                    state.update(recovery_mode=True, interval=180, next_at=stamp + 180)
                if identity == ids[10]:
                    state['starts'] = [stamp - 60 * i for i in range(30)]
                if identity == ids[11]:
                    state['halted'] = True
                pool.router._save(db, scope, state)

        pool.router.transaction_call(seed)
        stages = [(0, 'downloading'), (0, 'downloading'), (0, 'extracting'), (1, 'waiting_request'),
                  (2, 'extracting'), (3, 'waiting_transfer'), (5, 'converting'), (8, 'processing'),
                  (9, 'uploading_oss'), (None, 'waiting_cookie')]
        titles = ['海岸城市的清晨 · 4K 纪录片', '山野徒步路线与沿途风景', '古城街巷与人文旅行记录',
                  '雨天咖啡馆的环境音记录', '海边日落与城市延时摄影', '森林航拍与自然风光精选',
                  '2026 春季影像素材合集', '周末市集与街头影像记录', '城市建筑与航拍素材', '待分配的视频']
        task_ids = []
        for index, (account, stage) in enumerate(stages):
            task = store.create('download', {'urls': ['https://youtu.be/jNQXAC9IVRw']})
            task_ids.append(task['id'])
            progress = {'stage': stage, 'title': titles[index], 'speed': (8 - index / 10) * 1024**2,
                        '_reported_at': time.time(), 'last_bytes_at': time.time()}
            if account is not None:
                progress.update(cookie_profile_id=ids[account], cookie_profile_name=f'account{account + 1:02d}@example.test')
            with store.connect() as db:
                db.execute(tasks.update().where(tasks.c.id == task['id']).values(status='running', instance_id='preview',
                    worker_id='fixture', lease_until=time.time() + 600, progress=json.dumps(progress)))
                if account is not None:
                    scope = next(scope for identity, _, scope in pool.accounts if identity == ids[account])
                    db.execute(download_permits.insert().values(token='account-' + str(index),
                        resource=hashlib.sha256(scope.encode()).hexdigest(), task_id=task['id'], owner='fixture', units=1))
                if stage == 'downloading':
                    db.execute(download_permits.insert().values(token='transfer-' + str(index),
                        resource=hashlib.sha256(b'local-transfers:preview').hexdigest(), task_id=task['id'], owner='fixture', units=1))
            store.log_path(task['id']).write_text(f'account-task-log-{index}\n', encoding='utf-8')

        stopped = threading.Event()

        def beat():
            sample = {'time': time.time(), 'policy': policy_key(config), 'workers': 19, 'transfers': 12,
                      'browsers': 1, 'cookie_pool': pool.status()['capacity_summary']}
            _atomic(store.directory / 'throughput.json', sample)
            store.fleet.beat(throughput=sample)

        def heartbeat():
            while not stopped.wait(2):
                beat()

        beat()
        heartbeat_thread = threading.Thread(target=heartbeat, daemon=True)
        heartbeat_thread.start()
        server = make_server('127.0.0.1', 0, app, threaded=True, request_handler=QuietHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f'http://127.0.0.1:{server.server_port}'
        errors, writes = [], []
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True, **browser_options(pw.chromium))
                page = browser.new_page(viewport={'width': 1440, 'height': 1050}, reduced_motion='reduce')
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.on('request', lambda request: writes.append(request.url) if request.method in ('POST', 'PUT', 'DELETE') else None)
                page.goto(base + '/workbench?instance_id=preview')
                panel = page.get_by_role('region', name='账号任务与等待', exact=True)
                rows = panel.locator('.account-runtime-item')
                tab = lambda key: panel.locator(f'[data-stage="{key}"]')
                row = lambda index: panel.locator(f'[data-account-id="{ids[index]}"]')
                expect(rows).to_have_count(8)
                expect(panel.locator('.account-panel-title')).to_contain_text('19 个账号 · 9 个执行任务')
                expected_counts = {'all': 9, 'downloading': 2, 'extracting': 2, 'waiting_transfer': 1,
                                   'processing': 1, 'waiting_request': 1, 'uploading_oss': 1, 'other': 1}
                for key, count in expected_counts.items():
                    expect(tab(key).locator('.account-stage-count')).to_have_text(str(count))
                assert panel.get_by_role('combobox').count() == 1, 'Execution stage still uses a dropdown'
                panel.screenshot(path=str(output / 'accounts-desktop.png'))

                # Multiple tasks in one account count independently; unrelated counts remain visible.
                tab('downloading').click()
                expect(rows).to_have_count(1)
                expect(row(0)).to_contain_text('另 1 个任务')
                expect(panel.locator('.account-filter-note')).to_contain_text('分布在 1 个账号')
                expect(tab('extracting').locator('.account-stage-count')).to_have_text('2')
                panel.screenshot(path=str(output / 'accounts-filtered.png'))
                row(0).get_by_role('button', name='另 1 个任务 ↗').click()
                expect(row(0).locator('[data-account-task]')).to_have_count(3)
                # Details retain tasks from other stages and each task opens its own log.
                second = row(0).locator(f'[data-account-task="{task_ids[1]}"]')
                second.get_by_role('button', name=re_log(titles[1]), exact=True).click()
                expect(page.locator('#log-content')).to_contain_text('account-task-log-1')
                page.get_by_role('button', name='关闭对话框', exact=True).click()
                expect(row(0).get_by_role('button', name='account01@example.test 详情')).to_have_attribute('aria-expanded', 'true')
                row(0).get_by_role('button', name='account01@example.test 详情').click()

                # Other stages remain filterable, including future backend stage values.
                tab('other').click()
                expect(rows).to_have_count(1)
                expect(row(5)).to_contain_text('转换中')
                row(5).get_by_role('button', name='account06@example.test 详情').click()
                expect(row(5).locator('.runtime-details')).to_contain_text('有效成功')
                expect(row(5).locator('.runtime-details')).to_contain_text('时间到不会独立触发调整')
                row(5).get_by_role('button', name='account06@example.test 详情').click()
                tab('all').click()
                query = panel.get_by_label('搜索运行账号', exact=True)
                query.fill('  ACCOUNT01@  ')
                expect(rows).to_have_count(1)
                expect(tab('all').locator('.account-stage-count')).to_have_text('3')
                expect(tab('extracting').locator('.account-stage-count')).to_have_text('1')
                expect(tab('processing').locator('.account-stage-count')).to_have_text('0')
                tab('processing').click()
                expect(rows).to_have_count(0)
                expect(panel).to_contain_text('暂无符合条件的账号')
                tab('all').click()
                query.fill('')

                panel.get_by_role('button', name='账号第 3 页', exact=True).click()
                expect(rows).to_have_count(3)
                query.fill('account01')
                expect(rows).to_have_count(1)
                expect(panel.get_by_role('button', name='账号第 1 页', exact=True)).to_have_attribute('aria-current', 'page')
                query.fill('')
                panel.get_by_label('账号运行状态', exact=True).select_option('cooldown')
                expect(rows).to_have_count(1)
                expect(row(4)).to_contain_text('账号冷却中')
                expect(tab('all').locator('.account-stage-count')).to_have_text('0')
                panel.get_by_label('账号运行状态', exact=True).select_option('')
                expect(panel.locator('.account-totals')).to_contain_text('冷却中 1')
                tab('downloading').click()
                panel.get_by_role('button', name='筛选冷却中的账号', exact=True).click()
                expect(rows).to_have_count(1)
                expect(row(4)).to_contain_text('账号冷却中')
                expect(tab('all')).to_have_attribute('aria-pressed', 'true')
                panel.get_by_role('button', name='筛选冷却中的账号', exact=True).click()
                expect(rows).to_have_count(8)
                panel.get_by_label('账号运行状态', exact=True).select_option('ready')
                expect(panel.get_by_role('button', name='筛选冷却中的账号', exact=True)).to_contain_text('冷却中 1')
                panel.get_by_label('账号运行状态', exact=True).select_option('')

                # Polling must not close expanded details or drop keyboard focus.
                row(0).get_by_role('button', name='account01@example.test 详情').click()
                toggle = row(0).get_by_role('button', name='account01@example.test 详情')
                toggle.focus()
                with page.expect_response(lambda response: '/api/status' in response.url):
                    pass
                expect(toggle).to_be_focused()
                expect(toggle).to_have_attribute('aria-expanded', 'true')
                toggle.click()

                for width in (1440, 1024, 768, 390):
                    page.set_viewport_size({'width': width, 'height': 1050 if width > 390 else 844})
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), f'Overflow at {width}'
                    panel.screenshot(path=str(output / f'accounts-{width}.png'))
                page.set_viewport_size({'width': 1440, 'height': 1050})

                # A failed capacity request keeps historical counts visibly labelled and suppresses live claims.
                page.route('**/api/status', lambda route: route.abort())
                expect(panel.locator('.account-sample')).to_contain_text('上次记录', timeout=15000)
                expect(panel.locator('.runtime-speed')).to_have_count(0)
                expect(row(0)).to_contain_text('当前状态待确认')
                expect(panel.locator('.account-totals')).to_have_text('执行中 — 等待中 — 冷却中 — 空闲 —')
                expect(panel.get_by_role('button', name='筛选冷却中的账号', exact=True)).to_be_disabled()
                expect(row(0).get_by_role('button', name=re_log(titles[0]), exact=True)).to_be_disabled()
                row(5).get_by_role('button', name='account06@example.test 详情').click()
                expect(row(5).locator('.runtime-details')).not_to_contain_text('有效成功')
                panel.screenshot(path=str(output / 'accounts-stale.png'))
                page.unroute('**/api/status')
                expect(panel.locator('.account-sample')).to_contain_text('采样更新', timeout=15000)
                assert not writes, f'Viewing/filtering accounts mutated state: {writes}'
                assert not errors, errors
                browser.close()
            print(json.dumps({'ok': True, 'accounts': 19, 'assigned_tasks': 9, 'unassigned_tasks': 1,
                              'browser_errors': errors, 'screenshots': str(output)}, ensure_ascii=False))
        finally:
            stopped.set()
            heartbeat_thread.join(timeout=5)
            server.shutdown()
            server.server_close()
            store.db.engine.dispose()


def re_log(title):
    return '查看 ' + title + ' 的日志'


if __name__ == '__main__':
    main()
