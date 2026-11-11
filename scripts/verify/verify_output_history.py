"""Render the workbench's real output history on desktop and mobile with local fixtures."""
from datetime import datetime, timezone
from pathlib import Path
import sys
import threading
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from playwright.sync_api import expect, sync_playwright
from werkzeug.serving import make_server

from app import create_app
from scripts.verify.verify_web import QuietHandler
from Utils.runtime import ROOT
from webapp.browser import browser_options
from webapp.database import deliveries, download_sessions


def main():
    output = ROOT / '.runtime' / 'verify' / 'validation' / ('output-history-' + uuid.uuid4().hex[:8])
    output.mkdir(parents=True)
    config = output / 'config.yaml'
    config.write_text('path: videos\nproxy_mode: direct\n', encoding='utf-8')
    app = create_app(config, output / 'state', start_worker=False)
    store = app.extensions['task_store']
    now = time.time()
    errors = []
    with store.connect() as db:
        identity = store.statistics.start_session(db)
        db.execute(download_sessions.update().where(download_sessions.c.id == identity).values(
            started_at=datetime.fromtimestamp(now - 1800, timezone.utc).isoformat()))
        total = 0
        for index in range(30):
            size = [0, 18, 40, 72, 35, 0, 90, 58, 26, 115][index % 10] * 1_000_000
            if not size:
                continue
            total += size
            db.execute(deliveries.insert().values(id=uuid.uuid4().hex, seq=index + 1,
                session_id=identity, task_id=uuid.uuid4().hex, title='fixture', path='/already-deleted.mp4',
                size=size, created_at=datetime.fromtimestamp(now - (29 - index) * 60, timezone.utc).isoformat()))
        db.execute(download_sessions.update().where(download_sessions.c.id == identity).values(file_bytes=total))
    server = make_server('127.0.0.1', 0, app, threaded=True, request_handler=QuietHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, **browser_options(playwright.chromium))
            context = browser.new_context(viewport={'width': 1440, 'height': 1100}, reduced_motion='reduce', has_touch=True)
            page = context.new_page()
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.goto(f'http://127.0.0.1:{server.server_port}', wait_until='networkidle')
            expect(page.locator('.output-chart-point')).to_have_count(30)
            expect(page.locator('#stat-output-recent')).not_to_have_text('—')
            assert 'L' in page.locator('#output-chart-line-complete').get_attribute('d')
            assert 'L' in page.locator('#output-chart-line-current').get_attribute('d')
            page.locator('.output-chart-point').nth(10).hover()
            expect(page.locator('#output-chart-detail')).to_contain_text('Mbps')
            page.locator('.output-chart-point').nth(15).focus()
            expect(page.locator('.output-chart-point').nth(15)).to_have_attribute('aria-pressed', 'true')
            page.screenshot(path=str(output / 'desktop.png'), full_page=True)
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            # Real reload must reconstruct the same history without client samples.
            page.reload(wait_until='networkidle')
            expect(page.locator('.output-chart-point')).to_have_count(30)
            for width in (390, 320):
                page.set_viewport_size({'width': width, 'height': 844})
                page.locator('.output-chart-point').nth(20).tap()
                expect(page.locator('#output-chart-detail')).to_contain_text('Mbps')
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), width
                page.screenshot(path=str(output / f'mobile-{width}.png'), full_page=True)
            with store.connect() as db:
                db.execute(deliveries.delete().where(deliveries.c.session_id == identity))
                db.execute(download_sessions.update().where(download_sessions.c.id == identity).values(file_bytes=0))
            page.reload(wait_until='networkidle')
            expect(page.locator('#stat-output-recent')).to_have_text('0.00')
            page.screenshot(path=str(output / 'zero-output.png'), full_page=True)
            with store.connect() as db:
                store.statistics.stop_session(db)
            page.reload(wait_until='networkidle')
            expect(page.locator('#output-chart-empty')).to_be_visible()
            expect(page.locator('#output-chart-content')).to_be_hidden()
            expect(page.locator('#stat-output-recent')).to_have_text('—')
            page.screenshot(path=str(output / 'no-session.png'), full_page=True)
            assert not errors, errors
            browser.close()
        print('Output history UI passed: desktop, 390px, 320px, selection, reload, zero and no session.')
        print(output)
    finally:
        server.shutdown()
        store.db.engine.dispose()


if __name__ == '__main__':
    main()
