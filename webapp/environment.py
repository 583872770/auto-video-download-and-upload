"""Checks needed by the YouTube download providers."""
import importlib.util
import shutil


class DownloadEnvironmentRequired(RuntimeError):
    """Missing fallback prerequisites must pause work rather than fail videos."""


def require_downmp3_environment():
    if not shutil.which('ffprobe'):
        raise DownloadEnvironmentRequired('downmp3 需要 ffprobe 校验视频和音频，请安装 FFmpeg 或重新构建 Docker 镜像。')


def require_fallback_environment(config, base):
    """Check both providers locally; the caller validates Cookie over the task proxy."""
    from webapp.yt5s import require_browser_environment

    require_browser_environment(config, base)
    if importlib.util.find_spec('playwright') is None:
        raise DownloadEnvironmentRequired('下载器环境检查未通过，缺少：playwright。请安装依赖或重新构建 Docker 镜像。')
    require_native_environment(config)


def require_native_environment(config):
    missing = [module for module in ('yt_dlp', 'yt_dlp_ejs')
               if importlib.util.find_spec(module) is None]
    missing.extend(binary for binary in ('ffmpeg', 'ffprobe') if not shutil.which(binary))
    runtimes = config.get('js_runtimes')
    if runtimes is None:
        ready = shutil.which('deno') or shutil.which('node')
    else:
        ready = isinstance(runtimes, dict) and any(
            isinstance(options, dict) and shutil.which(str(options.get('path') or name))
            for name, options in runtimes.items() if name in ('deno', 'node', 'bun', 'quickjs'))
    if not ready:
        missing.append('JavaScript 运行时（Node.js / Deno）')
    if missing:
        raise DownloadEnvironmentRequired('下载器环境检查未通过，缺少：' + '、'.join(missing)
                                          + '。请安装依赖或重新构建 Docker 镜像后继续下载。')


def check_download_environment(config=None):
    missing = []
    from webapp.settings import download_methods
    api_only = download_methods(config or {}) == ['downmp3']
    modules = ('yaml', 'PIL', 'yt_dlp') if api_only else ('yaml', 'PIL', 'yt_dlp', 'yt_dlp_ejs')
    for module in modules:
        found = importlib.util.find_spec(module) is not None
        print(f'{module}: {"OK" if found else "缺失"}', flush=True)
        if not found:
            missing.append(module)
    for binary in ('ffprobe',) if api_only else ('ffmpeg', 'ffprobe'):
        location = shutil.which(binary)
        print(f'{binary}: {location or "缺失，请安装 FFmpeg 并加入 PATH"}', flush=True)
        if not location:
            missing.append(binary)
    if api_only:
        print('downmp3: 使用转换 API；无需浏览器、YouTube Cookie 或 JavaScript 运行时。', flush=True)
        return int(bool(missing))
    runtime = shutil.which('deno') or shutil.which('node')
    print(f'JavaScript runtime: {runtime or "缺失，请安装 Deno 或 Node.js 22+"}', flush=True)
    if not runtime:
        missing.append('JavaScript runtime')
    return int(bool(missing))


