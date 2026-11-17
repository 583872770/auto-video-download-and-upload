"""Checks needed by the YouTube download providers."""
import importlib.util
import shutil


class DownloadEnvironmentRequired(RuntimeError):
    """Missing source prerequisites must pause work rather than fail videos."""


def require_native_environment(config):
    missing = [module for module in ('yt_dlp', 'yt_dlp_ejs')
               if importlib.util.find_spec(module) is None]
    outputs = config.get('_delivery_contract', {}).get('outputs')
    binaries = () if outputs and set(outputs) <= {'thumbnail', 'metadata'} else ('ffmpeg', 'ffprobe')
    missing.extend(binary for binary in binaries if not shutil.which(binary))
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


def check_download_environment(config=None, base=None):
    try:
        require_native_environment(config or {})
    except DownloadEnvironmentRequired as error:
        print(str(error), flush=True)
        return 1
    print('yt-dlp、JavaScript 运行时及媒体处理工具已就绪。', flush=True)
    return 0
