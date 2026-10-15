"""Preflight, prepare and upload local videos through an explicit CLI."""
import argparse
import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
import importlib
import importlib.util
import math
import json
from pathlib import Path
import shutil
import subprocess
import tempfile

from Utils.config import DEFAULT_CONFIG, load_config, resolve_path, video_directory
from Utils.get_schedule_time import generate_schedule_within_7_days
from Utils.split_video import get_video_duration, segment_count, split_video_ffmpeg
from Utils.runtime import configure_runtime
from Utils.paths import io_path

PLATFORMS = {
    'douyin': ('Uploaders.DouyinUpload', 'DouYinVideo'),
    'kuaishou': ('Uploaders.KuaishouUpload', 'KuaiShouVideo'),
    'xigua': ('Uploaders.XiguaUpload', 'XiGuaVideo'),
}
COVER_SIZES = {'douyin': (1080, 1440), 'douyin_horizontal': (1280, 960),
               'kuaishou': (1280, 960), 'xigua': (1920, 1080)}
VIDEO_SUFFIXES = {'.mp4', '.mkv', '.webm', '.mov'}


def getFileNames(file_path):
    files = (path for path in Path(io_path(file_path)).iterdir()
             if path.is_file() and path.suffix.lower() in VIDEO_SUFFIXES)
    return [path.name for path in sorted(files, key=lambda path: (path.stat().st_mtime, path.name))]


def find_cover(video):
    video = Path(video)
    images = {path.name.lower(): path for path in Path(io_path(video.parent)).iterdir() if path.is_file()}
    for suffix in ('.webp', '.jpg', '.jpeg', '.png'):
        cover = images.get((video.stem + suffix).lower())
        if cover:
            return cover
    raise FileNotFoundError(f'视频缺少同名封面（webp/jpg/jpeg/png）：{video.name}')


@dataclass
class VideoPlan:
    source: Path
    cover: Path
    parts: int


def build_plan(config, base):
    root = Path(io_path(video_directory(config, base)))
    if not root.is_dir():
        raise FileNotFoundError(f'视频目录不存在：{root}，请先下载或修改 config.yaml')
    names = getFileNames(root)
    if not names:
        raise ValueError(f'没有找到视频：{root}')
    max_duration = float(config.get('segment_seconds', 180))
    plans = []
    for name in names:
        source = root / name
        cover = find_cover(source)
        plans.append(VideoPlan(source, cover, segment_count(get_video_duration(source), max_duration)))
    hours = config.get('time_list', '8,11,14,16,19,21')
    if isinstance(hours, str):
        hours = [int(value.strip()) for value in hours.split(',')]
    if not isinstance(hours, list):
        raise ValueError('time_list 必须是逗号分隔的小时或 YAML 整数列表')
    schedule = generate_schedule_within_7_days(*hours)
    total = sum(plan.parts for plan in plans)
    if total > len(schedule):
        raise ValueError(f'需要 {total} 个排期，7 天内只有 {len(schedule)} 个；请减少视频或增加 time_list')
    return plans, schedule[:total]


def check_environment():
    missing = []
    for module in ('yaml', 'PIL', 'yt_dlp', 'yt_dlp_ejs', 'playwright'):
        found = importlib.util.find_spec(module) is not None
        print(f'{module}: {"OK" if found else "缺失"}')
        if not found:
            missing.append(module)
    for binary in ('ffmpeg', 'ffprobe'):
        location = shutil.which(binary)
        print(f'{binary}: {location or "缺失，请安装 FFmpeg 并加入 PATH"}')
        if not location:
            missing.append(binary)
    runtime = shutil.which('deno') or shutil.which('node')
    print(f'JavaScript runtime: {runtime or "缺失，请安装 Deno 或 Node.js 22+"}')
    if not runtime:
        missing.append('JavaScript runtime')
    if importlib.util.find_spec('playwright'):
        from playwright.sync_api import sync_playwright
        from Uploaders.browser import browser_options
        with sync_playwright() as playwright:
            options = browser_options(playwright.chromium)
            executable = options.get('executable_path', playwright.chromium.executable_path)
            installed = Path(executable).is_file()
        print(f'Browser: {executable if installed else "缺失，请运行 python -m playwright install chromium"}')
        if not installed:
            missing.append('Chromium')
    return int(bool(missing))


def prepare_media(plans, config, platforms):
    from Utils.process_cover import enhance_cover
    jobs = []
    for plan in plans:
        folder = Path(io_path(plan.source.parent / f'{plan.source.name}.parts'))
        segments = split_video_ffmpeg(plan.source, folder, float(config.get('segment_seconds', 180)))
        covers = {}
        cover_keys = list(platforms)
        if 'douyin' in platforms:
            cover_keys.append('douyin_horizontal')
        for key in cover_keys:
            covers[key] = Path(io_path(folder / f'{key}.png'))
            with enhance_cover(plan.cover, COVER_SIZES[key]) as image:
                image.save(covers[key], format='PNG')
        jobs.extend((part, covers) for part in segments)
    return jobs


def write_prepared_manifest(jobs, schedule, config, base, platforms):
    if len(jobs) != len(schedule):
        raise ValueError('视频分段数量已变化，请重新预检查')
    target = Path(io_path(video_directory(config, base) / 'prepared-jobs.json'))
    payload = {
        'status': 'prepared',
        'generated_at': datetime.now().astimezone().isoformat(),
        'platforms': platforms,
        'jobs': [{'file': str(part), 'covers': {key: str(value) for key, value in covers.items()},
                  'publish_at': slot.isoformat()} for (part, covers), slot in zip(jobs, schedule)],
    }
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=target.parent,
                                         prefix='.prepared-', suffix='.tmp', delete=False) as output:
            temporary = Path(output.name)
            json.dump(payload, output, ensure_ascii=False, indent=2)
        temporary.replace(target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return target


async def publish_plan(plans, schedule, config, base, platforms):
    from Uploaders.browser import validate_storage_state
    from playwright.async_api import Error as BrowserError
    timeout = float(config.get('upload_timeout_seconds', 1800))
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError('upload_timeout_seconds 必须是正数')
    executable = resolve_path(base, config['browser_executable']) if config.get('browser_executable') else None
    if executable is not None and not executable.is_file():
        raise FileNotFoundError(f'指定的浏览器不存在：{executable}')
    cookies = {}
    if 'kuaishou' in platforms and config.get('kuaishou_visibility', 'public') not in ('public', 'friends', 'private'):
        raise ValueError('kuaishou_visibility 必须是 public/friends/private')
    if 'douyin' in platforms and config.get('douyin_visibility', 'public') not in ('public', 'friends', 'private'):
        raise ValueError('douyin_visibility 必须是 public/friends/private')
    for platform in platforms:
        cookies[platform] = resolve_path(base, config.get(f'{platform}_cookie', f'Cookies/{platform}.json'))
        if not cookies[platform].is_file():
            raise FileNotFoundError(f'{platform} 登录文件不存在，请先运行 --login {platform}')
        validate_storage_state(cookies[platform])
        tags = config.get(f'{platform}_tags', [])
        if not isinstance(tags, list) or any(not isinstance(tag, str) for tag in tags):
            raise ValueError(f'{platform}_tags 必须是字符串列表')
    jobs = prepare_media(plans, config, platforms)
    if len(jobs) != len(schedule):
        raise ValueError('视频分段数量已变化，请重新预检查')
    for (part, covers), publish_time in zip(jobs, schedule):
        for platform in platforms:
            if publish_time < datetime.now() + timedelta(hours=2):
                raise ValueError('准备视频耗时过长，排期已不足两小时，请重新预检查')
            module_name, class_name = PLATFORMS[platform]
            uploader = getattr(importlib.import_module(module_name), class_name)
            app = uploader(part.stem, str(part), str(covers[platform]),
                           config.get(f'{platform}_tags', []), publish_time, str(cookies[platform]))
            app.upload_timeout_seconds = timeout
            app.local_executable_path = str(executable) if executable else ''
            if platform == 'kuaishou':
                app.visibility = config.get('kuaishou_visibility', 'public')
            if platform == 'douyin':
                app.visibility = config.get('douyin_visibility', 'public')
                app.horizontal_cover_path = str(covers['douyin_horizontal'])
            try:
                receipt = await asyncio.wait_for(app.main(), timeout=timeout)
            except TimeoutError as error:
                raise RuntimeError(f'{platform} 超过 {timeout:g} 秒，结果未确认；请先核对平台再重试') from error
            except BrowserError as error:
                raise RuntimeError(f'{platform} 浏览器操作失败：{error}') from error
            if not isinstance(receipt, dict) or receipt.get('status') != 'confirmed' or receipt.get('platform') != platform:
                raise RuntimeError(f'{platform} 未返回发布成功确认，已停止后续任务')
            print(f'{platform} 发布已确认：{part.name}')


def main(argv=None):
    configure_runtime()
    parser = argparse.ArgumentParser(description='视频分段与上传；默认仅预览任务')
    parser.add_argument('--config', type=Path, default=DEFAULT_CONFIG)
    parser.add_argument('--platforms', nargs='+', choices=PLATFORMS)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--check', action='store_true', help='检查依赖和 FFmpeg')
    mode.add_argument('--dry-run', action='store_true', help='预览排期，不写文件、不上传（默认）')
    mode.add_argument('--prepare', action='store_true', help='生成真实分段、封面和任务清单，不登录或上传')
    mode.add_argument('--publish', action='store_true', help='分段、生成封面并发布视频')
    mode.add_argument('--login', choices=PLATFORMS, help='打开浏览器登录并保存 Cookie')
    args = parser.parse_args(argv)
    try:
        if args.check:
            return check_environment()
        config, base = load_config(args.config)
        if args.login:
            from playwright.async_api import Error as BrowserError
            module = importlib.import_module(PLATFORMS[args.login][0])
            cookie = resolve_path(base, config.get(f'{args.login}_cookie', f'Cookies/{args.login}.json'))
            cookie.parent.mkdir(parents=True, exist_ok=True)
            executable = resolve_path(base, config['browser_executable']) if config.get('browser_executable') else None
            try:
                asyncio.run(getattr(module, f'{args.login}_cookie_gen')(str(cookie), executable))
            except BrowserError as error:
                raise RuntimeError(f'{args.login} 登录浏览器操作失败：{error}') from error
            return 0
        platforms = args.platforms or config.get('platforms', ['douyin', 'kuaishou'])
        if not isinstance(platforms, list) or not platforms or any(p not in PLATFORMS for p in platforms):
            raise ValueError('platforms 必须是 douyin/kuaishou/xigua 组成的非空列表')
        platforms = list(dict.fromkeys(platforms))
        plans, schedule = build_plan(config, base)
        print(f'平台：{", ".join(platforms)}；共 {len(plans)} 个视频，{len(schedule)} 个分段')
        for plan in plans:
            print(f'{plan.source.name} -> {plan.parts} 段；封面：{plan.cover.name}')
        for index, slot in enumerate(schedule, 1):
            print(f'{index}: {slot:%Y-%m-%d %H:%M}')
        if args.prepare:
            jobs = prepare_media(plans, config, platforms)
            manifest = write_prepared_manifest(jobs, schedule, config, base, platforms)
            print(f'预处理完成，未发布。任务清单：{manifest}')
        elif args.publish:
            asyncio.run(publish_plan(plans, schedule, config, base, platforms))
        else:
            print('预览完成。确认任务后使用 --publish 执行。')
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError, TimeoutError) as error:
        parser.exit(1, f'执行失败：{error}\n')


if __name__ == '__main__':
    raise SystemExit(main())
