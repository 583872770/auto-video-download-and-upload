"""Exercise the built task search UI against an isolated real HTTP backend."""
import os
from pathlib import Path
import sys
import threading
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from playwright.sync_api import expect, sync_playwright
from sqlalchemy import update
from werkzeug.serving import make_server
from scripts.verify.support import QuietHandler
from tests.test_job_videos import JobVideoTests
from webapp.browser import browser_options
from webapp.database import tasks


def main():
    environment = {k: v for k, v in os.environ.items()
                   if not k.startswith(('VIDEO_', 'MYSQL_', 'LOGIN_', 'REDIS_'))}
    with patch.dict(os.environ, environment, clear=True):
        fixture = JobVideoTests()
        fixture.setUp()
        server = None
        try:
            job = fixture.create_job('\n'.join(f'V{i:010d}' for i in range(75)))
            with fixture.store.connect() as db:
                db.execute(update(tasks).where(tasks.c.batch_id == job,
                    tasks.c.video_id == 'V0000000074').values(title='Search title fixture'))
            server = make_server('127.0.0.1', 0, fixture.app, threaded=True,
                                 request_handler=QuietHandler)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(**browser_options(playwright.chromium))
                page = browser.new_page(viewport={'width': 1440, 'height': 1000})
                errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.goto(f'http://127.0.0.1:{server.server_port}/jobs/{job}?tab=videos')
                rows = page.locator('tbody tr').filter(has=page.get_by_role('link'))
                expect(rows).to_have_count(50)
                query = page.get_by_role('textbox', name='搜索视频')
                submit = page.get_by_role('button', name='查询 / 刷新')
                # Editing without submitting must not change the paging query.
                query.fill('Missing_ID0')
                page.get_by_role('button', name='下一页', exact=True).click()
                expect(rows).to_have_count(25)
                submit.click()
                expect(rows).to_have_count(0)
                expect(page.get_by_text('没有符合筛选条件的视频。')).to_be_visible()
                query.fill('https://youtu.be/V0000000074?t=5')
                submit.click()
                expect(rows).to_have_count(1)
                expect(rows.first).to_contain_text('V0000000074')
                query.fill('partial')
                submit.click()
                expect(page.get_by_role('alert')).to_contain_text('11 位')
                page.get_by_role('combobox', name='视频查询方式').select_option('title')
                query.fill('Search title')
                submit.click()
                expect(rows).to_have_count(1)
                expect(page.get_by_role('alert')).to_have_count(0)
                page.set_viewport_size({'width': 390, 'height': 844})
                expect(query).to_be_visible()
                expect(submit).to_be_visible()
                assert not errors, errors
                browser.close()
            print('Job search UI passed: paging, exact link, missing ID, validation, title, mobile, script errors')
        finally:
            if server:
                server.shutdown()
            fixture.store.db.engine.dispose()
            fixture.doCleanups()


if __name__ == '__main__':
    main()
