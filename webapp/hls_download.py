"""Recover YouTube's empty, sub-frame HLS tail without accepting missing media."""
import json
import math
from pathlib import Path
import subprocess
from urllib.parse import urljoin

from yt_dlp.downloader.hls import HlsFD
from yt_dlp.postprocessor.ffmpeg import FFmpegPostProcessor


MAX_EMPTY_TAIL_SECONDS = .05
MEDIA_DURATION_TOLERANCE = .25
MAX_MANIFEST_BYTES = 2 * 1024 * 1024


def empty_tail_candidate(manifest, info):
    """Only handle complete, plain VOD playlists; leave complex HLS native."""
    if (info.get('is_live') or info.get('format_index') is not None
            or info.get('extra_param_to_segment_url') or info.get('request_data')
            or info.get('section_start') is not None or info.get('section_end') is not None):
        return None
    lines = manifest.splitlines()
    if not lines or lines[0].strip() != '#EXTM3U':
        return None
    duration, duration_line = None, None
    segments = []
    ended = False
    allowed = ('#EXTM3U', '#EXT-X-VERSION:', '#EXT-X-TARGETDURATION:',
               '#EXT-X-MEDIA-SEQUENCE:', '#EXT-X-PLAYLIST-TYPE:', '#EXT-X-INDEPENDENT-SEGMENTS')
    for index, raw in enumerate(lines):
        line = raw.strip()
        if not line:
            continue
        if ended:
            return None
        if line == '#EXT-X-ENDLIST':
            if duration is not None:
                return None
            ended = True
        elif line.startswith('#EXTINF:'):
            if duration is not None:
                return None
            try:
                duration = float(line.split(':', 1)[1].split(',', 1)[0])
            except ValueError:
                return None
            if not math.isfinite(duration) or duration <= 0:
                return None
            duration_line = index
        elif line.startswith(allowed):
            continue
        elif line.startswith('#'):
            # Keys, byte ranges, maps, discontinuities, ads and unknown tags
            # require a different completeness check.
            return None
        else:
            if duration is None:
                return None
            segments.append((duration_line, index, duration, line))
            duration = None
    if not ended or len(segments) < 2 or segments[-1][2] > MAX_EMPTY_TAIL_SECONDS:
        return None
    expected = sum(segment[2] for segment in segments[:-1])
    if not math.isfinite(expected) or expected < 1:
        return None
    reference = info.get('duration')
    if reference is not None:
        try:
            if not math.isfinite(float(reference)) or abs(float(reference) - expected) > 1:
                return None
        except (ValueError, TypeError):
            return None
    start, end, _, uri = segments[-1]
    filtered = '\n'.join(line for index, line in enumerate(lines) if index not in (start, end)) + '\n'
    return uri, filtered, expected


def verify_tail_media(filename, info, duration, executable):
    """Probe only recovered files; normal downloads incur no subprocess cost."""
    if not executable:
        raise ValueError('需要 ffprobe 校验空尾片下载结果')
    result = subprocess.run([executable, '-v', 'error', '-show_entries',
        'stream=codec_type,codec_name,duration', '-of', 'json', str(filename)],
        capture_output=True, text=True, timeout=30)
    if result.returncode or result.stderr.strip():
        raise ValueError('音视频文件未通过 ffprobe 校验')
    streams = json.loads(result.stdout).get('streams') or []
    expected = {kind for kind, key in (('video', 'vcodec'), ('audio', 'acodec'))
                if info.get(key) not in (None, 'none')}
    if not expected:
        expected = {'video', 'audio'}
    for kind in expected:
        matching = [stream for stream in streams if stream.get('codec_type') == kind]
        if not matching:
            raise ValueError('下载结果缺少所需音视频轨道')
        for stream in matching:
            value = float(stream.get('duration', 'nan'))
            if (not stream.get('codec_name') or not math.isfinite(value)
                    or abs(value - duration) > MEDIA_DURATION_TOLERANCE):
                raise ValueError('音视频轨道时长与完整分片清单不符')


class YoutubeHlsFD(HlsFD):
    def real_download(self, filename, info_dict):
        local = dict(info_dict)
        manifest = local.get('hls_media_playlist_data')
        if not manifest:
            self.to_screen('[hlsnative] Downloading m3u8 manifest')
            with self.ydl.urlopen(self._prepare_url(local, local['url'])) as response:
                body = response.read(MAX_MANIFEST_BYTES + 1)
                local['url'] = response.url
            if len(body) > MAX_MANIFEST_BYTES:
                return super().real_download(filename, info_dict)
            manifest = body.decode('utf-8', 'ignore')
            local['hls_media_playlist_data'] = manifest
        candidate = empty_tail_candidate(manifest, local)
        if not candidate:
            return super().real_download(filename, local)
        uri, filtered, expected = candidate
        # A missing, throttled, truncated or nonempty response is never an
        # empty-tail success. Use the same cookie jar, headers and networking.
        with self.ydl.urlopen(self._prepare_url(local, urljoin(local['url'], uri))) as response:
            empty = (response.status == 200 and not response.read(1)
                     and response.headers.get('Content-Length') in (None, '0'))
        if not empty:
            return super().real_download(filename, local)
        executable = FFmpegPostProcessor(self.ydl).probe_executable
        if not executable:
            self.report_error('空尾片下载需要 ffprobe 校验，无法确认文件完整性')
            return False
        local['hls_media_playlist_data'] = filtered
        previous = self.params
        self.params = {**previous, 'skip_unavailable_fragments': False,
                       'continuedl': False, 'overwrites': True}
        try:
            # Validate a fresh assembly, never reuse fragments from an earlier
            # attempt that may have silently skipped a different segment.
            Path(self.temp_name(filename)).unlink(missing_ok=True)
            Path(self.ytdl_filename(filename)).unlink(missing_ok=True)
            if not super().real_download(filename, local):
                return False
            try:
                verify_tail_media(filename, local, expected, executable)
            except (OSError, ValueError, TypeError, subprocess.SubprocessError):
                # Do not let a later retry accept an already-existing bad file.
                Path(filename).unlink(missing_ok=True)
                self.report_error('空尾片下载结果未通过音视频轨道或时长校验，仍按下载失败处理')
                return False
            self.report_warning('已忽略经确认的极短空尾片；音视频轨道和时长校验通过')
            return True
        finally:
            self.params = previous
