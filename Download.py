"""Download only when explicitly invoked; importing this module is safe."""
import argparse
from pathlib import Path
import shutil
import yt_dlp

from Utils.config import DEFAULT_CONFIG, load_config, resolve_path, video_directory
from Utils.runtime import configure_runtime


def build_ydl_options(config, base):
    options = {
        'format': config.get('download_format', 'bestvideo+bestaudio/best'),
        'download_archive': str(resolve_path(base, 'videos.txt')),
        'fragment_retries': 3,
        'ignoreerrors': 'only_download',
        'outtmpl': str(video_directory(config, base) / '%(title).120B [%(id)s].%(ext)s'),
        'merge_output_format': 'mp4',
        'retries': 3,
        'extractor_retries': 2,
        'socket_timeout': 20,
        'writesubtitles': True,
        'writeautomaticsub': True,
        'writeinfojson': True,
        'writethumbnail': True,
        'concurrent_fragment_downloads': 16,
        'http_chunk_size': 10 * 1024 * 1024,
        'windowsfilenames': True,
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
    if config.get('youtube_cookie'):
        cookie = resolve_path(base, config['youtube_cookie'])
        if not cookie.is_file():
            raise FileNotFoundError(f'YouTube Cookie 文件不存在：{cookie}')
        options['cookiefile'] = str(cookie)
    return options


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
        options = build_ydl_options(config, base)
        options['simulate'] = args.simulate
        with yt_dlp.YoutubeDL(options) as downloader:
            return downloader.download(urls)
    except (OSError, ValueError, yt_dlp.utils.DownloadError) as error:
        parser.exit(1, f'下载失败：{error}\n')


if __name__ == '__main__':
    raise SystemExit(main())
