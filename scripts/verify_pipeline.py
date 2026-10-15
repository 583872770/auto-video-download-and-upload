"""Exercise actual HTTP download and media preparation without platform access."""
import argparse
import functools
import hashlib
import http.server
import json
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PIL import Image
import yaml
from Download import main as download
from main import COVER_SIZES, getFileNames, main as prepare
from Utils.runtime import ROOT, configure_runtime
from Utils.split_video import get_video_duration


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


def main(argv=None):
    parser = argparse.ArgumentParser(description='真实本地下载、字幕、元数据和媒体预处理验证')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args(argv)
    configure_runtime()
    for binary in ('ffmpeg', 'ffprobe'):
        if not shutil.which(binary):
            parser.exit(1, f'缺少 {binary}，请先安装 FFmpeg。\n')
    work = (args.output or ROOT / '.cache' / 'validation' / f'pipeline-{uuid.uuid4().hex[:12]}').resolve()
    fixture = work / 'fixture'
    fixture.mkdir(parents=True, exist_ok=False)
    source = fixture / 'source.mp4'
    subprocess.run([
        'ffmpeg', '-hide_banner', '-loglevel', 'error', '-nostdin', '-y',
        '-f', 'lavfi', '-i', 'testsrc=size=320x180:rate=10',
        '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=44100',
        '-t', '7', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'aac', str(source),
    ], check=True)
    Image.new('RGB', (640, 360), (35, 105, 165)).save(fixture / 'cover.png')
    (fixture / 'captions.en.vtt').write_text(
        'WEBVTT\n\n00:00.000 --> 00:07.000\nLocal end-to-end verification.\n', encoding='utf-8')
    (fixture / 'index.html').write_text('''<!doctype html><meta charset="utf-8">
<title>Pipeline validation</title><video controls poster="cover.png">
<source src="source.mp4" type="video/mp4">
<track kind="subtitles" src="captions.en.vtt" srclang="en" label="English"></video>''', encoding='utf-8')
    handler = functools.partial(QuietHandler, directory=str(fixture))
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    config_path = work / 'config.yaml'
    output = work / 'videos'
    settings = {
        'path': str(output), 'dir': '', 'proxy': '',
        'url': f'http://127.0.0.1:{server.server_port}/index.html',
        'segment_seconds': 3, 'time_list': [8, 11, 14, 16, 19, 21],
        'platforms': ['douyin', 'kuaishou'],
    }
    config_path.write_text(yaml.safe_dump(settings), encoding='utf-8')
    try:
        if download(['--config', str(config_path)]) != 0:
            raise RuntimeError('真实本地 HTTP 下载失败')
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    videos = getFileNames(output)
    if len(videos) != 1:
        raise RuntimeError(f'预期下载一个视频，实际为 {len(videos)}')
    downloaded = output / videos[0]
    digest = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()
    if digest(downloaded) != digest(source):
        raise RuntimeError('下载文件与 HTTP 源文件不一致')
    for pattern in ('*.info.json', '*.vtt'):
        if not list(output.glob(pattern)):
            raise RuntimeError(f'未生成 {pattern}')
    if prepare(['--config', str(config_path), '--prepare']) != 0:
        raise RuntimeError('媒体预处理失败')
    manifest = json.loads((output / 'prepared-jobs.json').read_text(encoding='utf-8'))
    if len(manifest['jobs']) != 3:
        raise RuntimeError('分段数量不正确')
    durations = []
    for job in manifest['jobs']:
        part = Path(job['file'])
        subprocess.run(['ffmpeg', '-v', 'error', '-i', str(part), '-f', 'null', '-'],
                       check=True, capture_output=True)
        durations.append(get_video_duration(part))
        for platform, cover in job['covers'].items():
            with Image.open(cover) as image:
                if image.size != COVER_SIZES[platform]:
                    raise RuntimeError(f'{platform} 封面尺寸不正确')
    report = {
        'http_download': 'passed', 'download_sha256': digest(downloaded),
        'metadata': 'passed', 'subtitles': 'passed', 'media_prepare': 'passed',
        'segment_decode': 'passed', 'segment_count': len(durations), 'durations': durations,
        'covers': 'passed', 'manifest': str(output / 'prepared-jobs.json'),
        'config': str(config_path), 'youtube_download': 'not_exercised_by_this_local_test',
        'platform_login': 'not_attempted', 'platform_publication': 'not_attempted',
    }
    report_path = work / 'report.json'
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'PIPELINE_VERIFIED: {report_path}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
