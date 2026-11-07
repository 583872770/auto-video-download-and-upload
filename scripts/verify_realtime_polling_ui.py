"""Compare UI polling against the pre-optimization assets on one fixture."""
from collections import Counter
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from unittest.mock import patch
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from playwright.sync_api import expect, sync_playwright
from werkzeug.serving import make_server

from app import create_app
from scripts.verify_cookie_runtime_ui import QuietHandler
from webapp.cookie_pool import CookiePool
from webapp.cookie_profiles import CookieProfiles
from webapp.database import tasks
from webapp.settings import save_settings
from webapp.throughput import policy_key

ROOT = Path(__file__).resolve().parents[1]
BASELINE_REF = 'aea090f'


def measure(browser, address, *, baseline=False):
    context = browser.new_context(viewport={'width': 1440, 'height': 1100})
    if baseline:
        assets = {f'/static/{name}': subprocess.check_output(
            ['git', 'show', f'{BASELINE_REF}:static/{name}'], cwd=ROOT).decode('utf-8')
            for name in ('app.js', 'fleet.js', 'concurrency.js', 'settings_tabs.js')}
        def previous(route):
            source = assets.get(urlsplit(route.request.url).path)
            if source is None:
                route.continue_()
            else:
                route.fulfill(status=200, body=source, content_type='text/javascript; charset=utf-8')
        context.route('**/static/**', previous)
    page = context.new_page()
    errors, responses = [], []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.goto(address + '/#overview')
    expect(page.locator('[data-concurrency-panel=overview] .cookie-runtime-card')).to_have_count(19)
    page.wait_for_timeout(3000)
    page.on('response', lambda response: responses.append((urlsplit(response.url).path, response.status))
            if '/api/' in response.url else None)
    started = time.monotonic()
    page.wait_for_timeout(20000)
    elapsed = time.monotonic() - started
    counts = dict(Counter(path for path, _ in responses))
    render = page.evaluate('''() => Array.from({length:10}, () => {
        const start=performance.now(); renderConcurrency(); return performance.now()-start;
    })''')
    result = dict(seconds=round(elapsed, 2), requests=counts, total=len(responses),
                  statuses=dict(Counter(status for _, status in responses)),
                  render_median_ms=round(statistics.median(render), 2), page_errors=errors)
    assert not errors, errors
    assert all(status == 200 for _, status in responses), result
    if baseline:
        context.close()
        return result
    assert not counts.get('/api/downloads/startup'), 'idle startup must not poll'
    # Repeated renders and countdown-only changes must preserve the actual nodes.
    assert page.evaluate('''() => {
        const panel=document.querySelector('[data-concurrency-panel=overview]');
        const task=panel.querySelector('[data-task]');
        const details=panel.querySelector('details');
        if (details) details.open=true;
        task.focus(); renderConcurrency(); renderConcurrency();
        return task===panel.querySelector('[data-task]') && document.activeElement===task
          && (!details || details.open);
    }''')
    assert page.evaluate('''() => {
        const target=document.createElement('div');
        const a='<span data-capacity-deadline="123">3 秒</span>';
        const b='<span data-capacity-deadline="123">2 秒</span>';
        return capacityMarkupChanged(target,a) && !capacityMarkupChanged(target,b)
          && capacityMarkupChanged(target,b.replace('123','124'));
    }''')
    return result, context, page, responses


def main():
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith(('VIDEO_', 'MYSQL_', 'YT5S_'))}
    with tempfile.TemporaryDirectory(prefix='realtime-ui-') as directory, patch.dict(os.environ, environment, clear=True):
        root = Path(directory)
        path = root / 'config.yaml'
        path.write_text('path: videos\nproxy_mode: direct\nworker_count: 64\nmin_free_gb: 0\n'
                        'builtin_transfer_limit: 12\n', encoding='utf-8')
        app = create_app(path, root / 'state', start_worker=False, role='hybrid',
                         instance_id='preview', database_url='sqlite:///' + (root / 'db.sqlite3').as_posix())
        store = app.extensions['task_store']
        library = CookieProfiles(root, store)
        identities = [library.add(f'Account {i}', (f'# HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tSID\tfixture{i}\n').encode())['id']
                      for i in range(19)]
        local = store.fleet.local_settings()
        config = {**local['config'], 'youtube_cookie_pool': identities, 'youtube_cookie_session_limit': 2,
                  'youtube_cookie': library.require(identities[0]).relative_to(root).as_posix()}
        save_settings(path, config)
        store.fleet.save_local(config, local['base'], local['clash_directory'])
        store.fleet.control('preview', {'action': 'resume'})
        pool = CookiePool(store, config, root, None, strict=False)
        task = store.create('download', {'urls': ['https://youtu.be/jNQXAC9IVRw']})
        progress = {'stage': 'extracting', 'title': 'Fixture task', 'cookie_profile_id': identities[0],
                    'cookie_profile_name': 'Account 0', '_reported_at': time.time()}
        with store.connect() as db:
            db.execute(tasks.update().where(tasks.c.id == task['id']).values(status='running',
                instance_id='preview', worker_id='fixture', lease_until=time.time() + 600,
                progress=json.dumps(progress)))
        stop = threading.Event()
        def beat():
            while not stop.is_set():
                store.fleet.beat(throughput={'time': time.time(), 'policy': policy_key(config),
                                            'cookie_pool': pool.status()['capacity_summary']})
                stop.wait(3)
        heartbeat = threading.Thread(target=beat, daemon=True)
        heartbeat.start()
        server = make_server('127.0.0.1', 0, app, threaded=True, request_handler=QuietHandler)
        # Wait for in-flight requests before disposing SQLite (Windows holds
        # open files until every request connection has returned to the pool).
        server.daemon_threads = False
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        address = f'http://127.0.0.1:{server.server_port}'
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True)
                before = measure(browser, address, baseline=True)
                after, context, page, responses = measure(browser, address)
                assert after['total'] <= before['total'] * .7, (before, after)
                # Background pages stop network polls; returning resumes them.
                page.evaluate("Object.defineProperty(document,'hidden',{configurable:true,get:()=>true})")
                page.wait_for_timeout(1500)
                mark = len(responses)
                page.wait_for_timeout(4000)
                assert len(responses) == mark, 'hidden page kept polling'
                page.evaluate("delete document.hidden; document.dispatchEvent(new Event('visibilitychange'))")
                # Startup in another tab is discovered by normal status polling.
                startup = app.extensions['download_startup']
                with startup.lock:
                    startup.value = {'state': 'running', 'stage': '', 'steps': [], 'started_at': time.time()}
                expect(page.locator('#startup-title')).to_contain_text('启动检查中', timeout=15000)
                mark = len(responses)
                page.wait_for_timeout(2500)
                assert any(path == '/api/downloads/startup' for path, _ in responses[mark:])
                with startup.lock:
                    startup.value.update(state='succeeded', finished_at=time.time())
                expect(page.locator('#startup-title')).to_contain_text('启动检查已完成', timeout=5000)
                page.wait_for_timeout(1200)
                mark = len(responses)
                page.wait_for_timeout(2500)
                assert not any(path == '/api/downloads/startup' for path, _ in responses[mark:])
                assert page.evaluate("""() => {
                    const stamp=performance.now(); applyStartup({state:'succeeded'},stamp);
                    applyStartup({state:'running'},stamp-1); return state.startup.state==='succeeded';
                }""")
                # Execution progress remains on the fast path.
                progress.update(stage='downloading', _reported_at=time.time(), speed=1024)
                with store.connect() as db:
                    db.execute(tasks.update().where(tasks.c.id == task['id']).values(progress=json.dumps(progress)))
                expect(page.locator('[data-concurrency-panel=overview] .cookie-task-stage')).to_contain_text('正在传输', timeout=6000)
                page.evaluate("setView('settings')")
                page.locator('#settings-tab-accounts').click()
                expect(page.locator('[data-youtube-pacing-runtime]')).to_contain_text('已保存的账号池共用启动间隔')
                page.evaluate("setView('overview')")
                expect(page.locator('[data-concurrency-panel=overview] .cookie-runtime-card')).to_have_count(19)
                assert not before['page_errors'] and not after['page_errors']
                context.close()
                browser.close()
            result = {'before': before, 'after': after, 'request_reduction_percent': round(100*(1-after['total']/before['total']), 1),
                      'regressions': ['focus/node identity', 'countdown', 'hidden page', 'startup discovery/completion',
                                      'out-of-order startup', 'progress update', 'view/settings navigation']}
            output = ROOT / '.cache/perf-audit-current/realtime-ui-results.json'
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
            print(json.dumps(result, ensure_ascii=False))
        finally:
            stop.set()
            heartbeat.join(timeout=5)
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()
            store.db.engine.dispose()


if __name__ == '__main__':
    main()
