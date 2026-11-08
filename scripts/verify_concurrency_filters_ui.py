"""Exercise Cookie filters in the real page, with isolated diagnostic snapshots."""
from copy import deepcopy
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from playwright.sync_api import expect, sync_playwright
from werkzeug.serving import make_server

from app import create_app
from scripts.verify_cookie_runtime_ui import QuietHandler


def fixture():
    def account(index, stages=(), health='ready', known=True, reason='unassigned'):
        return dict(id=f'cookie-{index}', name=f'account{index + 1:02d}@example.test', known=known,
                    state=health, active=len(stages), limit=2, interval=30,
                    reasons=[dict(code=reason, label={
                        'unassigned': '尚未分配新任务', 'account_cooldown': '账号冷却中',
                        'pool_cooldown': '账号池冷却中', 'pool_halted': '账号池已拦截，需人工检查',
                        'unavailable': 'Cookie 文件不可用', 'shared': '与其他 Cookie 共用账号额度',
                    }.get(reason, reason))],
                    tasks=[dict(task_id=f'task-{index}-{number}', stage=stage,
                                title=f'视频 {index + 1}-{number + 1}', speed=1024**2,
                                transfer_reserved=stage == 'downloading') for number, stage in enumerate(stages)])
    accounts = [
        account(0, ('downloading', 'waiting_transfer')),
        account(1, ('downloading',), 'cooldown', reason='account_cooldown'),
        account(2, ('extracting',), 'recovery'),
        account(3, ('waiting_request',)),
        account(4, ('processing', 'waiting_processing')),
        account(5), account(6, health='unavailable', reason='unavailable'),
        account(7, ('downloading',), health='unknown', known=False),
        account(8, ('new_unrecognized_stage',)),
        account(9, ('cleaning', 'uploading_oss')),
        account(10, ('waiting_site_form',)),
        account(11, ('preparing_workspace', 'converting')),
        account(12, reason='pool_halted'), account(13, reason='pool_cooldown'),
        {**account(14, reason='shared'), 'shared_with': 'cookie-0', 'shared_name': 'account01@example.test'},
    ]
    return dict(online=True, requested=12, transfer_limit=12, effective_limit=12,
                connection_budget=32, mode='cookie_pool', sample_fresh=True, disk_sample_fresh=True,
                cookie_pool=dict(known=True, configured_files=15, effective_sessions=14, capacity=24),
                transfer_allocation=dict(configured=12, effective=12, occupied=[{}, {}, {}],
                                         unoccupied=9, restricted=0),
                cookie_allocation=dict(accounts=accounts, pool_start_interval=5,
                    unassigned_tasks=[dict(task_id='unassigned', stage='waiting_cookie', title='待关联的视频')]))


def main():
    output = Path(__file__).resolve().parents[1] / '.cache/concurrency-filters-preview'
    output.mkdir(parents=True, exist_ok=True)
    environment = {k: v for k, v in os.environ.items() if not k.startswith(('VIDEO_', 'MYSQL_', 'YT5S_'))}
    with tempfile.TemporaryDirectory(prefix='cookie-filters-') as temporary, patch.dict(os.environ, environment, clear=True):
        root = Path(temporary)
        config = root / 'config.yaml'
        config.write_text('path: videos\nproxy_mode: direct\nmin_free_gb: 0\n', encoding='utf-8')
        app = create_app(config, root / 'state', start_worker=False, role='hybrid', instance_id='preview',
                         database_url='sqlite:///' + (root / 'fixture.sqlite3').as_posix())
        store = app.extensions['task_store']
        store.fleet.beat()
        server = make_server('127.0.0.1', 0, app, threaded=True, request_handler=QuietHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        runtime = fixture()
        sample = {'age': 0}
        errors = []
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True)
                page = browser.new_page(viewport={'width': 1440, 'height': 1100})
                page.on('pageerror', lambda error: errors.append(str(error)))

                def diagnostic(route):
                    response = route.fetch()
                    body = response.json()
                    for item in body.get('items', []):
                        if item['id'] == 'preview':
                            item['concurrency'] = {**deepcopy(runtime), 'time': time.time() - sample['age']}
                    route.fulfill(response=response, json=body)

                page.route('**/api/instances*', diagnostic)
                page.goto(f'http://127.0.0.1:{server.server_port}/#overview')
                panel = page.locator('[data-concurrency-panel=overview]')
                cards = panel.locator('.cookie-runtime-card')
                button = lambda key: panel.locator(f'[data-cookie-task-filter="{key}"]')
                selector = panel.locator('[data-cookie-account-filter]')
                card = lambda index: panel.locator(f'[data-cookie-id="cookie-{index}"]')
                summary = panel.locator('.cookie-filter-summary')
                expect(cards).to_have_count(15)
                for key, count in dict(all=15, transferring=3, preparing=2, waiting=4, processing=2, idle=5).items():
                    expect(button(key).locator('b')).to_have_text(str(count))
                expect(selector.locator('option[value=normal]')).to_contain_text('9')
                expect(selector.locator('option[value=cooldown]')).to_contain_text('2')
                expect(selector.locator('option[value=manual]')).to_contain_text('1')

                # A cooling account can still transmit; a mixed account keeps both tasks.
                button('transferring').click()
                expect(cards).to_have_count(3)
                expect(card(0).locator('.cookie-task')).to_have_count(2)
                expect(card(0)).to_contain_text('等待传输名额')
                selector.select_option('cooldown')
                expect(cards).to_have_count(1)
                expect(card(1)).to_be_visible()
                expect(button('all').locator('b')).to_have_text('2')
                expect(button('idle').locator('b')).to_have_text('1')
                expect(summary).to_contain_text('1 / 15')
                expect(panel.locator('.transfer-effective')).to_contain_text('12')
                expect(panel.locator('.transfer-allocation-heading')).to_contain_text('已占用 3 路')
                expect(panel.locator('.cookie-unassigned-tasks')).to_contain_text('待关联的视频')

                selector.select_option('recovery')
                expect(cards).to_have_count(0)
                expect(panel.locator('.cookie-filter-empty')).to_be_visible()
                panel.locator('[data-cookie-filters-clear]').click()
                expect(cards).to_have_count(15)
                expect(button('all')).to_be_focused()
                expect(selector).to_have_value('all')

                # Every primary category applies to tasks, not the card's priority badge.
                for key, count in (('preparing', 2), ('waiting', 4), ('processing', 2), ('idle', 5)):
                    button(key).click()
                    expect(cards).to_have_count(count)
                expect(card(8)).to_have_count(0)  # Unrecognized running stage is not idle.
                button('all').click()
                for key, count in (('normal', 9), ('cooldown', 2), ('recovery', 1),
                                   ('manual', 1), ('unavailable', 1), ('unknown', 1)):
                    selector.select_option(key)
                    expect(cards).to_have_count(count)
                # Fresh tasks can be known even when their Cookie health is not.
                expect(card(7).locator('.cookie-task')).to_have_count(1)
                selector.select_option('all')
                button('waiting').click()
                selector.focus()
                page.evaluate('window.filterControl = document.activeElement')
                runtime['cookie_allocation']['accounts'][3]['tasks'][0]['stage'] = 'downloading'
                page.evaluate('loadFleet()')
                expect(cards).to_have_count(3)
                expect(button('waiting')).to_have_attribute('aria-pressed', 'true')
                expect(button('transferring').locator('b')).to_have_text('4')
                assert page.evaluate('document.activeElement === window.filterControl'), 'poll replaced focused dropdown'
                expect(panel.locator('.transfer-allocation-heading')).to_contain_text('已占用 3 路')

                # Preferences belong to the selected executor and survive returning to it.
                assert page.evaluate('''() => {
                    const selected=state.fleet.items.find(item=>item.id===state.instanceId);
                    state.fleet.items.push({...selected,id:'filter-other'});
                    const own=state.instanceId;
                    try {
                        state.instanceId='filter-other'; renderConcurrency();
                        const panel=document.querySelector('[data-concurrency-panel=overview]');
                        if(panel.querySelector('[data-cookie-task-filter][aria-pressed=true]').dataset.cookieTaskFilter!=='all') return false;
                        panel.querySelector('[data-cookie-task-filter=idle]').click();
                        state.instanceId=own; renderConcurrency();
                        return panel.querySelector('[data-cookie-task-filter][aria-pressed=true]').dataset.cookieTaskFilter==='waiting';
                    } finally {state.instanceId=own;state.fleet.items.pop();renderConcurrency();}
                }''')

                # Expired snapshots never turn old transmissions into current idle accounts.
                sample['age'] = 60
                page.evaluate('loadFleet()')
                expect(panel.locator('.cookie-filter-empty')).to_be_visible()
                expect(button('waiting')).to_have_attribute('aria-pressed', 'true')
                expect(button('idle').locator('b')).to_have_text('0')
                panel.locator('[data-cookie-filters-clear]').click()
                expect(panel.locator('.cookie-runtime-card.unknown')).to_have_count(15)
                expect(panel.locator('.cookie-task')).to_have_count(0)
                expect(selector.locator('option[value=unknown]')).to_contain_text('15')
                sample['age'] = 0
                runtime['online'] = False
                page.evaluate('loadFleet()')
                expect(button('idle').locator('b')).to_have_text('0')
                runtime['online'] = True
                page.evaluate('loadFleet()')
                expect(button('idle').locator('b')).to_have_text('5')

                # Filtering is synchronous view work and issues no fetch requests.
                assert page.evaluate('''() => {
                    const original=window.fetch;let calls=0;
                    window.fetch=(...args)=>{calls++;return original(...args)};
                    try {
                        const panel=document.querySelector('[data-concurrency-panel=overview]');
                        panel.querySelector('[data-cookie-task-filter=transferring]').click();
                        const select=panel.querySelector('[data-cookie-account-filter]');
                        select.value='cooldown';select.dispatchEvent(new Event('change',{bubbles:true}));
                        panel.querySelector('[data-cookie-filters-clear]').click();
                        return calls===0;
                    } finally {window.fetch=original;}
                }''')
                button('transferring').click()
                panel.screenshot(path=str(output / 'filtered-desktop.png'))
                for width in (390, 360):
                    page.set_viewport_size({'width': width, 'height': 844})
                    page.locator('#sidebar').evaluate('node=>Promise.all(node.getAnimations().map(a=>a.finished))')
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), f'overflow at {width}px'
                panel.screenshot(path=str(output / 'filtered-mobile.png'))
                selector.select_option('recovery')
                expect(panel.locator('.cookie-filter-empty')).to_be_visible()
                panel.screenshot(path=str(output / 'empty-mobile.png'))
                assert not errors, errors
                browser.close()
        finally:
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()
            store.db.engine.dispose()
        print(json.dumps(dict(result='PASS', accounts=15, scenarios=[
            'mixed-tasks', 'cooling-transfer', 'cross-counts', 'all-states', 'unknown-stage',
            'empty-reset', 'live-update-focus', 'instance-preferences', 'stale-offline',
            'whole-instance-totals', 'unassigned-tasks', 'no-added-fetch', 'desktop-mobile'],
            page_errors=errors, output=str(output)), ensure_ascii=False))


if __name__ == '__main__':
    main()
