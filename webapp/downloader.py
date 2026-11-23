"""Download only when explicitly invoked; importing this module is safe."""
from contextlib import contextmanager
from pathlib import Path
import shutil
import sys
import tempfile

from Utils.config import resolve_path, video_directory
from webapp.concurrency_policy import fragment_limit


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
        'format': 'bestvideo*+bestaudio/best',
        'format_sort': ['res', 'fps'],
        'download_archive': None if test_mode or config.get('storage_mode') == 'oss' else str(resolve_path(base, 'videos.txt')),
        'fragment_retries': 3,
        'ignoreerrors': 'only_download',
        'outtmpl': output_template(config, base),
        'merge_output_format': 'mp4/mkv',
        'retries': 3,
        'extractor_retries': 2,
        'socket_timeout': 20,
        # Limit terminal formatting only. Structured hooks still receive every
        # native event, including fragment progress and completion.
        'progress_delta': 1,
        'noprogress': True,
        # Subtitle outputs are selected by the job delivery contract only.
        'writesubtitles': False,
        'writeautomaticsub': False,
        'writeinfojson': True,
        'writethumbnail': True,
        'concurrent_fragment_downloads': fragment_limit(config),
        'http_chunk_size': 10 * 1024 * 1024,
        'windowsfilenames': True,
        'cachedir': False,
        '_temporary_directory': config.get('_temporary_directory'),
        '_parallel_http': True,
    }
    if config.get('youtube_sleep_preset', True):
        # Space extractor requests and all output file starts. Captions inherit
        # the same file-start range in ParallelYoutubeDL, without a second policy.
        options.update(sleep_interval_requests=.75, sleep_interval=10,
                       max_sleep_interval=20)
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
        if config.get('youtube_source_address'):
            options['source_address'] = config['youtube_source_address']
    elif config.get('proxy_mode') == 'clash':
        if not config.get('_resolved_clash_proxy'):
            raise ValueError('请通过 Web 页面创建使用 Clash 订阅代理的下载任务。')
        options['proxy'] = config['_resolved_clash_proxy']
    if config.get('youtube_cookie'):
        cookie = resolve_path(base, config['youtube_cookie'])
        if not cookie.is_file():
            raise FileNotFoundError(f'YouTube Cookie 文件不存在：{cookie}')
        options['cookiefile'] = str(cookie)
    from webapp.subtitle_tracks import provider_options
    from webapp.player_cache import cache_options
    options.update(cache_options(config))
    return provider_options(options)
