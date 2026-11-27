"""Recover YouTube's empty, sub-frame HLS tail without accepting missing media."""
import json
import math
from contextlib import nullcontext
from pathlib import Path
import subprocess
from threading import get_ident
from urllib.parse import urljoin

from yt_dlp.downloader.hls import HlsFD
from yt_dlp.postprocessor.ffmpeg import FFmpegPostProcessor

from webapp.hls_buffer import (BufferedFragmentHttpFD, FragmentBuffers,
                               OrderedFragmentPool, buffer_slot)


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
        'stream=codec_type,codec_name,duration:stream_disposition=attached_pic', '-of', 'json', str(filename)],
        capture_output=True, text=True, timeout=30)
    if result.returncode or result.stderr.strip():
        raise ValueError('音视频文件未通过 ffprobe 校验')
    streams = json.loads(result.stdout).get('streams') or []
    expected = {kind for kind, key in (('video', 'vcodec'), ('audio', 'acodec'))
                if info.get(key) not in (None, 'none')}
    if not expected:
        expected = {'video', 'audio'}
    for kind in expected:
        matching = [stream for stream in streams if stream.get('codec_type') == kind
                    and not stream.get('disposition', {}).get('attached_pic')]
        if not matching:
            raise ValueError('下载结果缺少所需音视频轨道')
        for stream in matching:
            value = float(stream.get('duration', 'nan'))
            if (not stream.get('codec_name') or not math.isfinite(value)
                    or abs(value - duration) > MEDIA_DURATION_TOLERANCE):
                raise ValueError('音视频轨道时长与完整分片清单不符')


class _AssemblyStream:
    """Only the assembler may close the file shared by fragment workers."""

    def __init__(self, stream):
        self.stream = stream
        self.owner = get_ident()

    def __getattr__(self, name):
        return getattr(self.stream, name)

    def close(self):
        # A later fragment may exhaust its retries while an earlier fragment
        # is still being appended. The assembler will observe the failure.
        if get_ident() == self.owner:
            self.stream.close()


class StrictHlsFD(HlsFD):
    def __init__(self, ydl, params):
        super().__init__(ydl, {**params, 'skip_unavailable_fragments': False})

    def _prepare_frag_download(self, ctx):
        partial = Path(self.temp_name(ctx['filename']))
        checkpoint = Path(self.ytdl_filename(ctx['filename']))
        if partial.exists() or checkpoint.exists():
            # Parallel download progress can advance the checkpoint ahead of
            # the fragments actually written to disk. Restart the assembly.
            prefix = partial.name + '-Frag'
            for item in partial.parent.iterdir():
                if item.name.startswith(prefix) and item.name[len(prefix):].removesuffix('.part').isdigit():
                    item.unlink(missing_ok=True)
            partial.unlink(missing_ok=True)
            checkpoint.unlink(missing_ok=True)
        return super()._prepare_frag_download(ctx)

    def _read_fragment(self, ctx):
        buffers = ctx.get('fragment_buffers')
        if buffers is None:
            return super()._read_fragment(ctx)
        return buffers.files.get(ctx.get('fragment_filename_sanitized'))

    def _append_fragment(self, ctx, frag_content):
        buffers = ctx.get('fragment_buffers')
        if buffers is None:
            return super()._append_fragment(ctx, frag_content)
        filename = ctx.pop('fragment_filename_sanitized')
        try:
            stream = ctx['dest_stream']
            frag_content.append_to(stream)
            stream.flush()
            if frag_content.disk:
                buffers.disk_fragments += 1
            else:
                buffers.memory_fragments += 1
        finally:
            buffers.files.pop(filename).discard()
            if frag_content.disk:
                self.try_remove(filename)

    def download_and_append_fragments(self, ctx, fragments, info_dict, **kwargs):
        stream = ctx['dest_stream']
        ctx['dest_stream'] = _AssemblyStream(stream)
        try:
            directory = self.params.get('_hls_buffer_directory')
            if (not directory or ctx.get('live') or info_dict.get('ext') == 'vtt'
                    or self.params.get('keep_fragments') or kwargs
                    or any(f['decrypt_info']['METHOD'] != 'NONE' for f in fragments)):
                return super().download_and_append_fragments(ctx, fragments, info_dict, **kwargs)
            workers = math.ceil(self.params.get('concurrent_fragment_downloads', 1) / ctx.get('max_progress', 1))
            with buffer_slot(directory) as reserved:
                buffers = ctx['fragment_buffers'] = FragmentBuffers(reserved)
                ctx['dl'] = BufferedFragmentHttpFD(ctx['dl'], buffers)
                pool = OrderedFragmentPool(workers) if workers > 1 else nullcontext()
                # Strict HLS restarts any partial assembly. The startup marker
                # stays available for cleanup, with no checkpoint per fragment.
                params = self.params
                self.params = {**params, '_no_ytdl_file': True}
                try:
                    with pool as executor:
                        success = super().download_and_append_fragments(ctx, fragments, info_dict, tpe=executor)
                    if success:
                        self.try_remove(self.ytdl_filename(ctx['filename']))
                    return success
                finally:
                    self.params = params
                    buffers.clear()
                    self.to_screen(f'[hlsnative] 分片缓冲峰值 {buffers.peak / 1024**2:.2f} MiB，'
                                   f'内存 {buffers.memory_fragments} 片，磁盘 {buffers.disk_fragments} 片。')
        finally:
            ctx['dest_stream'] = stream
            if not stream.closed:
                stream.close()


class YoutubeHlsFD(StrictHlsFD):
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
        self.params = {**previous, 'continuedl': False, 'overwrites': True}
        try:
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
