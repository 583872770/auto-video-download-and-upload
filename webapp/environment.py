"""Checks needed for video downloading, independent of publishing browsers."""
import importlib.util
import shutil


def check_download_environment():
    missing = []
    for module in ('yaml', 'PIL', 'yt_dlp', 'yt_dlp_ejs'):
        found = importlib.util.find_spec(module) is not None
        print(f'{module}: {"OK" if found else "缺失"}', flush=True)
        if not found:
            missing.append(module)
    for binary in ('ffmpeg', 'ffprobe'):
        location = shutil.which(binary)
        print(f'{binary}: {location or "缺失，请安装 FFmpeg 并加入 PATH"}', flush=True)
        if not location:
            missing.append(binary)
    runtime = shutil.which('deno') or shutil.which('node')
    print(f'JavaScript runtime: {runtime or "缺失，请安装 Deno 或 Node.js 22+"}', flush=True)
    if not runtime:
        missing.append('JavaScript runtime')
    return int(bool(missing))
