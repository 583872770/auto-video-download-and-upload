"""Bounded HTTP Range downloads using yt-dlp's networking and progress hooks."""
from collections import deque
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import suppress
from contextlib import contextmanager
import os
import random
from pathlib import Path
import threading
import time

import yt_dlp
from yt_dlp.downloader import get_suitable_downloader
from yt_dlp.downloader.http import HttpFD
from yt_dlp.downloader.hls import HlsFD
from yt_dlp.networking import Request
from yt_dlp.networking.exceptions import CertificateVerifyError, HTTPError, IncompleteRead, TransportError
from yt_dlp.utils import parse_http_range, sanitize_path
from yt_dlp.utils.networking import HTTPHeaderDict
from webapp.diagnostics import safe_text
from webapp.hls_download import StrictHlsFD, YoutubeHlsFD


CHUNK_SIZE = 10 * 1024 * 1024


class RangeUnsupported(Exception):
    """The source cannot safely serve independent byte ranges."""


def transport_error_message(error):
    detail = safe_text(str(error))[:400]
    text = detail.lower()
    reason = ('TLS 证书验证失败' if isinstance(error, CertificateVerifyError) else
              '连接读取超时' if 'timed out' in text or 'timeout' in text else
              '分块响应不完整' if isinstance(error, IncompleteRead) or 'incomplete http range' in text else
              '连接传输中断')
    return f'{reason}（{type(error).__name__}）' + (f'：{detail}' if detail else '')


class ParallelHttpFD(HttpFD):
    FD_NAME = 'parallel-http'

    @HttpFD.wrap_file_access('open', fatal=True)
    def open_range_file(self, filename, resume):
        # yt-dlp's locked_file supports only r/w/a, not update modes. Open
        # without O_APPEND or O_TRUNC, acquire the exclusive lock, then reset
        # fresh downloads. This also preserves an existing resume prefix.
        filename = sanitize_path(filename)
        flags = os.O_RDWR | os.O_CREAT | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_CLOEXEC', 0)
        stream = os.fdopen(os.open(filename, flags, 0o666), 'r+b')
        try:
            if os.name != 'nt':
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if not resume:
                stream.truncate(0)
            return stream, filename
        except BaseException:
            stream.close()
            raise

    def real_download(self, filename, info_dict):
        try:
            try:
                return self._parallel_download(filename, info_dict)
            except TransportError as error:
                if isinstance(error, CertificateVerifyError):
                    raise
                # All original workers and responses have stopped by now.
                # Retry once with a single connection, using the same signed
                # URL, cookie jar and route, without another task admission.
                self.report_warning(f'HTTP 并行下载出现{transport_error_message(error)}；降为单连接重新下载一次。')
                previous = self.params
                self.params = {**previous, 'concurrent_fragment_downloads': 1, 'continuedl': False}
                try:
                    # A new probe can observe a different object or trigger
                    # native fallback; neither may append to the old prefix.
                    Path(self.temp_name(filename)).unlink(missing_ok=True)
                    return self._parallel_download(filename, info_dict)
                finally:
                    self.params = previous
        except RangeUnsupported:
            # A changed object or invalid range must never be combined with
            # bytes from another response. All parallel workers have stopped.
            self.to_screen('[download] HTTP 范围响应不兼容，回退顺序下载。')
            return super().real_download(filename, info_dict)
        except (HTTPError, TransportError) as error:
            # Preserve the HTTP status for the shared cooldown classifier,
            # without copying signed URLs or authorization headers into logs.
            status = f'HTTP Error {error.status}' if isinstance(error, HTTPError) else transport_error_message(error)
            if isinstance(error, HTTPError):
                error.retry_after = error.response.headers.get('Retry-After')
                error.close()
                if error.status == 416:
                    self.to_screen('[download] HTTP 范围探测不兼容，回退顺序下载。')
                    return super().real_download(filename, info_dict)
            self.ydl.http_range_error = error
            self.report_error(f'HTTP 分块下载失败：{status}')
            return False

    def _parallel_download(self, filename, info):
        headers = HTTPHeaderDict(info.get('http_headers'), {'Accept-Encoding': 'identity'})
        with self.ydl.urlopen(Request(info['url'], headers={**headers, 'Range': 'bytes=0-0'})) as probe:
            start, end, total = parse_http_range(probe.headers.get('Content-Range'))
            if (probe.status != 206 or (start, end) != (0, 0) or not total
                    or probe.headers.get('Content-Encoding', 'identity').lower() != 'identity'):
                raise RangeUnsupported()
            validator_header = 'ETag'
            validator = probe.headers.get(validator_header)
            if not validator or validator.startswith('W/'):
                validator_header = 'Last-Modified'
                validator = probe.headers.get(validator_header)
            modified = probe.headers.get('Last-Modified')
        for option, rejected in (('min_filesize', lambda value: total < value),
                                 ('max_filesize', lambda value: total > value)):
            if self.params.get(option) is not None and rejected(self.params[option]):
                self.to_screen(f'[download] File size rejected by {option} ({total} bytes).')
                return False

        chunk_size = int(self.params.get('http_chunk_size') or CHUNK_SIZE)
        tmpfilename = self.temp_name(filename)
        resume = (self.filesize_or_none(tmpfilename) or 0) if self.params.get('continuedl', True) else 0
        if resume > total:
            raise RangeUnsupported()
        if resume:
            self.report_resuming_byte(resume)
        # Positional writes must not use O_APPEND: Linux pwrite on an append
        # descriptor ignores its offset. Only a verified contiguous prefix is
        # retained on cancellation, so the next attempt can resume by length.
        stream, tmpfilename = self.open_range_file(tmpfilename, resume)
        self.report_destination(filename)
        workers = min(16, max(1, int(self.params.get('concurrent_fragment_downloads', 1))))
        self.to_screen(f'[download] HTTP 分块并行：最多 {workers} 个连接，每块 {chunk_size / 1024**2:g} MiB。')
        stop = threading.Event()
        lock = threading.Lock()
        responses = set()
        received = {}
        completed = {}
        contiguous = resume
        incompatible = False
        started = time.monotonic()
        samples = deque([(started, resume)])
        last_progress = started - 1

        def progress(force=False):
            nonlocal last_progress
            now = time.monotonic()
            if not force and now - last_progress < .25:
                return
            with lock:
                downloaded = contiguous + sum(received.values())
            samples.append((now, downloaded))
            while len(samples) > 2 and samples[1][0] <= now - 5:
                samples.popleft()
            elapsed = now - samples[0][0]
            speed = (downloaded - samples[0][1]) / elapsed if elapsed >= .001 else None
            self._hook_progress(dict(status='downloading', downloaded_bytes=downloaded,
                total_bytes=total, speed=speed, eta=(total - downloaded) / speed if speed else None,
                elapsed=now - started, filename=filename, tmpfilename=tmpfilename,
                ctx_id=info.get('ctx_id')), info)
            last_progress = now

        def fetch(offset, end):
            request_headers = HTTPHeaderDict(headers, {'Range': f'bytes={offset}-{end}'})
            if validator:
                request_headers['If-Range'] = validator
            retries = self.params.get('fragment_retries', 3)
            attempt = 0
            while not stop.is_set():
                try:
                    with self.ydl.urlopen(Request(info['url'], headers=request_headers)) as response:
                        with lock:
                            responses.add(response)
                        try:
                            if (response.status != 206 or parse_http_range(response.headers.get('Content-Range')) != (offset, end, total)
                                    or response.headers.get('Content-Encoding', 'identity').lower() != 'identity'
                                    or (validator and response.headers.get(validator_header) not in (None, validator))):
                                raise RangeUnsupported()
                            expected = end - offset + 1
                            written = 0
                            while written < expected and not stop.is_set():
                                block = response.read(min(256 * 1024, expected - written))
                                if not block:
                                    raise TransportError('Incomplete HTTP range')
                                # Bound retained media to one small block per
                                # connection instead of a whole 10 MiB range.
                                # The lock covers only local seek/write, never
                                # network reads or callbacks.
                                with lock:
                                    stream.seek(offset + written)
                                    view = memoryview(block)
                                    while view:
                                        count = stream.write(view)
                                        if not count:
                                            raise OSError('无法写入下载临时文件。')
                                        view = view[count:]
                                    written += len(block)
                                    received[offset] = max(received.get(offset, 0), written)
                            if stop.is_set():
                                return
                            if response.read(1):
                                raise RangeUnsupported()
                            return offset, end + 1
                        finally:
                            with lock:
                                responses.discard(response)
                except HTTPError as error:
                    error.close()
                    # A range that becomes unsatisfiable can indicate that the
                    # resource changed. Never retry it into the existing file.
                    if error.status == 416:
                        raise RangeUnsupported() from error
                    if error.status not in (408, 500, 502, 503, 504):
                        raise
                    if error.response.headers.get('Retry-After'):
                        raise
                    if attempt >= retries:
                        raise
                except TransportError:
                    if attempt >= retries:
                        raise
                attempt += 1
                if stop.wait(min(2, .25 * attempt)):
                    return

        # Keep at most `workers` range requests in flight. Futures retain only
        # offsets; data goes straight into the temporary file in small blocks.
        pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix='http-range')
        pending = set()
        next_offset = resume

        def fill():
            nonlocal next_offset
            # 慢分块只限制前探距离，已经结束的连接仍可领取下一块。
            window_end = min(total, contiguous + 2 * workers * chunk_size)
            while len(pending) < workers and next_offset < window_end:
                end = min(total - 1, next_offset + chunk_size - 1)
                pending.add(pool.submit(fetch, next_offset, end))
                next_offset = end + 1

        try:
            progress(force=True)
            fill()
            while pending:
                finished, pending = wait(pending, timeout=.2, return_when=FIRST_COMPLETED)
                errors = []
                for future in finished:
                    error = future.exception()
                    if error is not None:
                        errors.append(error)
                    else:
                        offset, end = future.result()
                        completed[offset] = end
                # 只折叠已校验的连续前缀，限制进度账本和稀疏后缀的大小。
                with lock:
                    while contiguous in completed:
                        received.pop(contiguous, None)
                        contiguous = completed.pop(contiguous)
                if errors:
                    raise next((error for error in errors if isinstance(error, RangeUnsupported)), errors[0])
                fill()
                progress()
            stream.flush()
            if contiguous != total:
                raise RangeUnsupported()
        except RangeUnsupported:
            # The native fallback must restart, not append to an object whose
            # identity/range support was disproven by one of the responses.
            incompatible = True
            raise
        finally:
            stop.set()
            for future in pending:
                future.cancel()
            with lock:
                active = list(responses)
            for response in active:
                with suppress(Exception):
                    response.close()
            pool.shutdown(wait=True, cancel_futures=True)
            try:
                # No writer is alive now. Discard sparse/unverified suffixes;
                # they must never masquerade as a complete resumable prefix.
                stream.truncate(0 if incompatible else contiguous)
            finally:
                stream.close()

        self.try_rename(tmpfilename, filename)
        if ((tmpfilename != filename and os.path.exists(tmpfilename))
                or not os.path.isfile(filename) or os.path.getsize(filename) != total):
            self.report_error('HTTP 分块下载完成后文件大小校验失败。')
            return False
        if self.params.get('updatetime'):
            info['filetime'] = self.try_utime(filename, modified)
        self._hook_progress(dict(status='finished', downloaded_bytes=total, total_bytes=total,
            filename=filename, elapsed=time.monotonic() - started, ctx_id=info.get('ctx_id')), info)
        return True


class ParallelYoutubeDL(yt_dlp.YoutubeDL):
    """Use bounded HTTP transfers and verified empty-tail HLS recovery."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from webapp.player_cache import install
        install(self)

    def close(self):
        if getattr(self, '_delivery_closed', False):
            return
        self._delivery_closed = True
        try:
            super().close()
        finally:
            # Native close handles sockets/cookies but retains extractors,
            # public player code and callbacks. Workers reuse the process,
            # never the downloader: end those object graphs at this boundary.
            for ie in self._ies_instances.values():
                for name in ('_code_cache', '_player_cache'):
                    value = getattr(ie, name, None)
                    if isinstance(value, dict):
                        value.clear()
            self._ies.clear()
            self._ies_instances.clear()
            self._pps.clear()
            self._progress_hooks.clear()
            self._postprocessor_hooks.clear()
            self._post_hooks.clear()
            self._close_hooks.clear()
            self.format_selector = None
            for key in ('progress_hooks', 'postprocessor_hooks', 'post_hooks', 'logger'):
                self.params.pop(key, None)

    @contextmanager
    def file_observation(self, fd, name):
        yield

    def _download_file(self, fd_type, name, info, subtitle):
        if not info.get('url'):
            self.raise_no_formats(info, True)
        new_info = self._copy_infodict(info)
        if new_info.get('http_headers') is None:
            new_info['http_headers'] = self._calc_headers(new_info)
        fd = fd_type(self, self.params)
        with self.file_observation(fd, name):
            for hook in self._progress_hooks:
                fd.add_progress_hook(hook)
            return fd.download(name, new_info, subtitle)

    def dl(self, name, info, subtitle=False, test=False):
        if test:
            return super().dl(name, info, subtitle, test)
        if subtitle:
            # yt-dlp uses a separate parameter for captions. Derive it from
            # the common file-start policy instead of imposing another delay.
            minimum = self.params.get('sleep_interval') or 0
            self.params['sleep_interval_subtitles'] = random.uniform(
                minimum, self.params.get('max_sleep_interval') or minimum)
        native = get_suitable_downloader(info, self.params, to_stdout=(name == '-'))
        if (native is HlsFD and not subtitle and name != '-' and not hasattr(name, 'write')
                and not self.params.get('test') and not info.get('is_live')):
            youtube = info.get('extractor_key') == 'Youtube' or info.get('extractor') == 'youtube'
            return self._download_file(YoutubeHlsFD if youtube else StrictHlsFD, name, info, subtitle)
        eligible = (self.params.get('_parallel_http') and not subtitle and not test
            and name != '-' and not hasattr(name, 'write')
            and self.params.get('concurrent_fragment_downloads', 1) > 1
            and not self.params.get('ratelimit') and not self.params.get('test')
            and not self.params.get('impersonate') and not info.get('impersonate')
            and not info.get('is_live') and not info.get('request_data')
            and info.get('section_start') is None and info.get('section_end') is None
            and (info.get('filesize') or 0) > int(self.params.get('http_chunk_size') or CHUNK_SIZE)
            and 'Range' not in HTTPHeaderDict(info.get('http_headers')))
        return self._download_file(ParallelHttpFD if eligible and native is HttpFD else native,
                                   name, info, subtitle)
