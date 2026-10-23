"""Exercise the real local Flask UI in Chrome, with an isolated config and queue."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PIL import Image
from flask import send_file
from playwright.sync_api import expect, sync_playwright
from werkzeug.serving import WSGIRequestHandler, make_server
import yaml

from app import create_app
from Uploaders.browser import browser_options
from Utils.runtime import ROOT, configure_runtime


class QuietHandler(WSGIRequestHandler):
    def log_request(self, *args, **kwargs):
        pass


def main():
    configure_runtime()
    if not shutil.which('ffmpeg'):
        raise RuntimeError('浏览器播放验证需要 FFmpeg 生成本地测试视频。')
    output = ROOT / '.cache' / 'validation' / f'web-{uuid.uuid4().hex[:10]}'
    output.mkdir(parents=True)
    config = output / 'config.yaml'
    config.write_text(yaml.safe_dump({
        'path': 'videos', 'platforms': ['douyin', 'kuaishou'],
        'douyin_visibility': 'private', 'kuaishou_visibility': 'private',
    }), encoding='utf-8')
    fixture = output / '本地测试素材.mp4'
    subprocess.run(['ffmpeg', '-v', 'error', '-nostdin', '-y',
                    '-f', 'lavfi', '-i', 'testsrc2=size=320x180:rate=24:duration=3',
                    '-f', 'lavfi', '-i', 'sine=frequency=440:duration=3',
                    '-c:v', 'libx264', '-preset', 'ultrafast', '-pix_fmt', 'yuv420p',
                    '-c:a', 'aac', '-movflags', '+faststart', str(fixture)], check=True)
    image = output / 'fixture.png'
    Image.new('RGB', (640, 360), '#a5b48e').save(image)
    cookie = output / 'test-cookies.txt'
    cookie.write_bytes(b'# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tSID\tlocal-test-only\n')
    app = create_app(config, output / 'state')
    app.add_url_rule('/test-source.mp4', view_func=lambda: send_file(fixture, mimetype='video/mp4'))
    server = make_server('127.0.0.1', 0, app, threaded=True, request_handler=QuietHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    errors = []
    report = {}
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, **browser_options(playwright.chromium))
            try:
                context = browser.new_context(viewport={'width': 1440, 'height': 1080}, device_scale_factor=1,
                                              reduced_motion='reduce')
                page = context.new_page()
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.on('console', lambda message: errors.append(message.text) if message.type == 'error' else None)
                page.goto(f'http://127.0.0.1:{server.server_port}', wait_until='networkidle')
                page.get_by_text('还没有下载的视频', exact=True).wait_for()
                assert page.get_by_text('抖音', exact=True).count() == 0
                assert page.get_by_text('快手', exact=True).count() == 0
                assert page.locator('#publish-button, #preview-button, #prepare-button, [data-login]').count() == 0
                report['download_only_interface'] = 'passed'
                page.screenshot(path=str(output / 'desktop.png'), full_page=True)
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                page.locator('.nav-item[data-view="settings"]').click()
                page.locator('input[name="dir"]').fill('youtube-downloads')
                page.get_by_role('button', name='保存设置', exact=True).click()
                page.get_by_text('设置已保存，后续任务将使用新的配置。', exact=True).wait_for()
                assert yaml.safe_load(config.read_text(encoding='utf-8'))['dir'] == 'youtube-downloads'
                assert page.locator('input[name="segment_seconds"], input[name="time_list"], select[name$="visibility"]').count() == 0
                report['settings_roundtrip'] = 'passed'
                page.locator('.nav-item[data-view="overview"]').click()
                page.locator('.youtube-cookie-row [data-open]').click()
                page.locator('#youtube-cookie-form input[name="cookie"]').set_input_files(str(cookie))
                page.get_by_role('button', name='上传并保存', exact=True).click()
                expect(page.locator('#youtube-cookie-dialog')).not_to_be_visible()
                expect(page.locator('.youtube-cookie-row [data-youtube-status]')).to_have_text('Cookie 已保存 · 有效性以下载结果为准')
                assert yaml.safe_load(config.read_text(encoding='utf-8'))['youtube_cookie'] == 'Cookies/youtube.txt'
                assert (output / 'Cookies' / 'youtube.txt').is_file()
                report['youtube_cookie_file_upload'] = 'passed'
                page.locator('#download-urls').fill(f'http://127.0.0.1:{server.server_port}/test-source.mp4')
                page.locator('#download-form button[type="submit"]').click()
                page.locator('#batch-progress .batch-video').first.wait_for()
                page.locator('#batch-progress [data-task]').first.click()
                page.locator('#task-dialog[open]').wait_for()
                page.locator('#task-detail-meta .status-succeeded').wait_for(timeout=30000)
                page.locator('#task-dialog [data-close]').click()
                page.locator('.nav-item[data-view="media"]').click()
                page.locator('#all-media .media-card').first.wait_for()
                downloaded = list((output / 'videos' / 'youtube-downloads').rglob('*.mp4'))
                assert len(downloaded) == 1 and downloaded[0].read_bytes() == fixture.read_bytes()
                report['browser_download_and_media_library'] = 'passed'
                stem = downloaded[0].stem
                shutil.copy2(image, downloaded[0].with_suffix('.png'))
                # Leftover format tracks must not duplicate the finished item.
                (downloaded[0].parent / f'{stem}.f136.mp4').write_bytes(b'leftover-video-track')
                (downloaded[0].parent / f'{stem}.f251.webm').write_bytes(b'leftover-audio-track')
                page.locator('#refresh-media').click()
                expect(page.locator('#task-dialog')).to_be_visible()
                page.locator('#task-dialog [data-close]').click()
                expect(page.locator('#all-media .media-card')).to_have_count(1)
                page.wait_for_function('() => document.querySelector("#all-media .media-card img")?.naturalWidth > 0')
                page.locator('#all-media .media-card').click()
                expect(page.locator('#media-dialog')).to_be_visible()
                page.wait_for_function('() => document.querySelector("#media-player").currentTime > 0.1')
                assert page.locator('#media-player').evaluate('(video) => video.duration > 0 && video.videoWidth > 0 && !video.muted')
                page.locator('#media-player').evaluate('(video) => { video.currentTime = 1; }')
                page.wait_for_function('() => document.querySelector("#media-player").currentTime > 1.1')
                expect(page.locator('#media-player-error')).not_to_be_visible()
                page.screenshot(path=str(output / 'player.png'), full_page=True)
                page.set_viewport_size({'width': 390, 'height': 844})
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                page.screenshot(path=str(output / 'player-mobile.png'), full_page=True)
                page.set_viewport_size({'width': 1440, 'height': 1080})
                page.locator('#media-dialog [data-close]').click()
                assert page.locator('#media-player').evaluate('(video) => video.paused && !video.getAttribute("src")')
                report['single_item_cover_playback_and_seeking'] = 'passed'
                page.locator('.nav-item[data-view="overview"]').click()
                page.locator('#check-environment').click()
                page.locator('#task-dialog[open]').wait_for()
                page.locator('#task-detail-meta .status-succeeded, #task-detail-meta .status-failed').wait_for(timeout=30000)
                expect(page.locator('#task-log')).to_contain_text('ffmpeg', timeout=10000)
                assert '任务开始' in page.locator('#task-log').inner_text()
                report['background_task_and_live_logs'] = 'passed'
                page.locator('#task-dialog [data-close]').click()
                page.locator('.nav-item[data-view="tasks"]').click()
                page.locator('#all-tasks .task-row').first.wait_for()
                page.reload(wait_until='networkidle')
                page.locator('#all-tasks .task-row').first.wait_for()
                report['history_after_reload'] = 'passed'
                page.locator('.nav-item[data-view="overview"]').click()
                page.set_viewport_size({'width': 390, 'height': 844})
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), 'mobile horizontal overflow'
                page.screenshot(path=str(output / 'mobile.png'), full_page=True)
                page.locator('#mobile-menu').click()
                page.locator('.nav-item[data-view="settings"]').click()
                assert page.locator('#settings-view').is_visible()
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                report['responsive_navigation'] = 'passed'
                assert not errors, errors
                report['browser_errors'] = errors
                context.close()
            finally:
                browser.close()
    finally:
        server.shutdown()
        server.server_close()
        app.extensions['task_runner'].stop.set()
        process = app.extensions['task_runner'].process
        if process:
            process.wait(timeout=15)
    report['screenshots'] = [str(output / name) for name in ('desktop.png', 'mobile.png', 'player.png', 'player-mobile.png')]
    (output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'WEB_VERIFIED: {output / "report.json"}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
