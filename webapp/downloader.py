"""Download only when explicitly invoked; importing this module is safe."""
import argparse
from contextlib import contextmanager
from pathlib import Path
import shutil
import sys
import tempfile
from urllib.parse import urlsplit
import yt_dlp

from Utils.config import DEFAULT_CONFIG, load_config, resolve_path, video_directory
from Utils.runtime import configure_runtime


def output_template(config, base):
    directory = video_directory(config, base)
    shard = config.get('shard_downloads')
    title_bytes = 120
    if sys.platform == 'win32':
        # Metadata is written via <name>.info.json.<random>.tmp. Format tracks,
        # subtitles and fragment downloads add suffixes too. Budget the entire
        # path, including job/attempt directories, before truncating the title.
        directory_length = len(str(directory).encode('utf-16-le')) // 2
        if shard:
            directory_length += 15  # /<two characters>/<11-character YouTube ID>
        title_bytes = min(title_bytes, 240 - directory_length - 1 - 48)
        if title_bytes < 1:
            raise ValueError('下载目录过长，无法为视频和元数据临时文件预留空间。'
                             '请在当前实例的系统设置中缩短素材目录或子目录。')
    if shard:
        directory = directory / '%(id).2s' / '%(id)s'
    return str(directory / f'%(title).{title_bytes}B [%(id)s].%(ext)s')


@contextmanager
def download_cookie_session(options):
    """Let yt-dlp update its cookie jar without overwriting the uploaded login."""
    if not options.get('cookiefile'):
        yield options
        return
    temporary_root = options.get('_temporary_directory')
    if temporary_root:
        Path(temporary_root).mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='youtube-download-', dir=temporary_root) as directory:
        cookie = Path(directory) / 'cookies.txt'
        shutil.copyfile(options['cookiefile'], cookie)
        yield {**options, 'cookiefile': str(cookie)}


def build_ydl_options(config, base):
    from Utils.download_storage import storage_mode
    test_mode = storage_mode(config) == 'test'
    options = {
        'format': config.get('download_format') or 'bestvideo*+bestaudio/best',
        'format_sort': ['res', 'fps'],
        'download_archive': None if test_mode else str(resolve_path(base, 'videos.txt')),
        'fragment_retries': 3,
        'ignoreerrors': 'only_download',
        'outtmpl': output_template(config, base),
        'merge_output_format': 'mp4/mkv',
        'retries': 3,
        'extractor_retries': 2,
        'socket_timeout': 20,
        'writesubtitles': config.get('subtitle_mode', 'optional') == 'required',
        'writeautomaticsub': config.get('subtitle_mode', 'optional') == 'required',
        'subtitleslangs': [value.strip() for value in config.get('subtitle_languages', 'en,zh-Hans,zh-Hant').split(',')],
        'writeinfojson': True,
        'writethumbnail': True,
        'concurrent_fragment_downloads': int(config.get('fragment_concurrency', 4)),
        'http_chunk_size': 10 * 1024 * 1024,
        'windowsfilenames': True,
        'cachedir': False,
        '_temporary_directory': config.get('_temporary_directory'),
    }
    if config.get('js_runtimes') is not None:
        if not isinstance(config['js_runtimes'], dict):
            raise ValueError('js_runtimes 必须是运行时配置表')
        options['js_runtimes'] = config['js_runtimes']
    elif shutil.which('deno'):
        options['js_runtimes'] = {'deno': {'path': shutil.which('deno')}}
    elif shutil.which('node'):
        options['js_runtimes'] = {'node': {'path': shutil.which('node')}}
    if config.get('proxy') is not None:
        options['proxy'] = config['proxy']
    if config.get('proxy_mode') == 'direct':
        options['proxy'] = ''
    elif config.get('proxy_mode') == 'clash':
        if not config.get('_resolved_clash_proxy'):
            raise ValueError('请通过 Web 页面创建使用 Clash 订阅代理的下载任务。')
        options['proxy'] = config['_resolved_clash_proxy']
    if config.get('download_method', 'yt_dlp') in ('yt5s', 'downmp3', 'yt1d', 'bergung'):
        options.update(format='best', writesubtitles=False, writeautomaticsub=False, ignoreerrors=False)
    elif config.get('youtube_cookie'):
        cookie = resolve_path(base, config['youtube_cookie'])
        if not cookie.is_file():
            raise FileNotFoundError(f'YouTube Cookie 文件不存在：{cookie}')
        options['cookiefile'] = str(cookie)
    return options


def require_download_tools(urls, options):
    youtube = any((urlsplit(url).hostname or '').lower().removeprefix('www.') in
                  ('youtube.com', 'm.youtube.com', 'music.youtube.com', 'youtu.be') for url in urls)
    if youtube and '+' in options['format'] and not shutil.which('ffmpeg'):
        raise RuntimeError('YouTube 音视频合并需要 FFmpeg，请先安装后再下载，避免留下分离轨道。')


def main(argv=None):
    configure_runtime()
    parser = argparse.ArgumentParser(description='下载视频、封面、元数据和字幕')
    parser.add_argument('urls', nargs='*', help='视频或播放列表 URL；默认读取配置中的 url')
    parser.add_argument('--config', type=Path, default=DEFAULT_CONFIG)
    parser.add_argument('--output', help='覆盖配置中的视频输出目录')
    parser.add_argument('--proxy', help='仅为本次下载设置代理')
    parser.add_argument('--simulate', action='store_true', help='联网解析信息但不下载文件')
    args = parser.parse_args(argv)
    try:
        config, base = load_config(args.config)
        if config.get('download_method') in ('downmp3', 'yt1d', 'bergung'):
            raise ValueError('downmp3、yt1d、Bergung 请通过 Web 页面下载，以记录解析进度和下载统计。')
        urls = args.urls or config.get('url', [])
        if isinstance(urls, str):
            urls = [urls]
        if not isinstance(urls, list) or not urls or any(
            not isinstance(url, str) or not url.startswith(('https://', 'http://'))
            or 'xxxxxxxxxxx' in url for url in urls
        ):
            raise ValueError('请提供有效的视频 URL，或修改 config.yaml 中的 url')
        if args.output:
            config.update(path=args.output, dir='')
        if args.proxy is not None:
            config['proxy'] = args.proxy
            config['proxy_mode'] = 'manual'
        from Utils.download_storage import DownloadWorkspace
        workspace = DownloadWorkspace(config, base / '.cache')
        try:
            options = build_ydl_options(workspace.prepare(), base)
            if not args.simulate:
                require_download_tools(urls, options)
            options['simulate'] = args.simulate
            with download_cookie_session(options) as session_options:
                with yt_dlp.YoutubeDL(session_options) as downloader:
                    return downloader.download(urls)
        finally:
            workspace.cleanup()
    except (OSError, ValueError, RuntimeError, yt_dlp.utils.DownloadError) as error:
        parser.exit(1, f'下载失败：{error}\n')


if __name__ == '__main__':
    raise SystemExit(main())

