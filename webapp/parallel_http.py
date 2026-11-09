"""Bounded HTTP Range downloads using yt-dlp's networking and progress hooks."""
from collections import deque
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from contextlib import suppress
import os
from pathlib import Path
import threading
import time

import yt_dlp
from yt_dlp.downloader import get_suitable_downloader
from yt_dlp.downloader.http import HttpFD
from yt_dlp.downloader.hls import HlsFD
from yt_dlp.networking import Request
from yt_dlp.networking.exceptions import CertificateVerifyError, HTTPError, IncompleteRead, TransportError
from yt_dlp.utils import parse_http_range
from yt_dlp.utils.networking import HTTPHeaderDict
from webapp.concurrency_policy import fragment_connection_budget
from webapp.diagnostics import safe_text
from webapp.hls_download import YoutubeHlsFD


CHUNK_SIZE = 10 * 1024 * 1024


def transfer_connection_units(info, budget, *, native=False):
    formats = info.get('requested_formats') or [info]
    protocols = [item.get('protocol') or item.get('url', '').split(':', 1)[0] for item in formats]
    parallel = any(any(word in protocol for word in ('m3u8', 'dash', 'ism'))
                   or (native and protocol in ('http', 'https')) for protocol in protocols)
    return budget if parallel else 1


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
        stream, tmpfilename = self.sanitize_open(tmpfilename, 'ab' if resume else 'wb')
        self.report_destination(filename)
        workers = min(16, max(1, int(self.params.get('concurrent_fragment_downloads', 1))))
        self.to_screen(f'[download] HTTP 分块并行：最多 {workers} 个连接，每块 {chunk_size / 1024**2:g} MiB。')
        stop = threading.Event()
        lock = threading.Lock()
        responses = set()
        received = {}
        started = time.monotonic()
        samples = deque([(started, resume)])
        last_progress = started - 1

        def progress(force=False):
            nonlocal last_progress
            now = time.monotonic()
            if not force and now - last_progress < .25:
                return
            with lock:
                downloaded = resume + sum(received.values())
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
                            data = bytearray()
                            expected = end - offset + 1
                            while len(data) < expected and not stop.is_set():
                                block = response.read(min(256 * 1024, expected - len(data)))
                                if not block:
                                    raise TransportError('Incomplete HTTP range')
                                data.extend(block)
                                with lock:
                                    received[offset] = max(received.get(offset, 0), len(data))
                            if stop.is_set():
                                return
                            if response.read(1):
                                raise RangeUnsupported()
                            return data
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

        # Keep at most `workers` chunks in flight/buffered. Unlike submitting
        # the whole file, a slow first chunk cannot exhaust memory or disk.
        pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix='http-range')
        pending = deque()
        next_offset = resume

        def submit():
            nonlocal next_offset
            if next_offset < total:
                end = min(total - 1, next_offset + chunk_size - 1)
                pending.append(pool.submit(fetch, next_offset, end))
                next_offset = end + 1

        try:
            progress(force=True)
            for _ in range(workers):
                submit()
            while pending:
                future = pending[0]
                try:
                    data = future.result(timeout=.2)
                except FutureTimeout:
                    if future.done():
                        data = future.result()
                    else:
                        progress()
                        continue
                stream.write(data)
                pending.popleft()
                submit()
                progress()
            stream.flush()
            if stream.tell() != total:
                raise RangeUnsupported()
        except RangeUnsupported:
            # The native fallback must restart, not append to an object whose
            # identity/range support was disproven by one of the responses.
            stream.seek(0)
            stream.truncate()
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
    def dl(self, name, info, subtitle=False, test=False):
        if (not subtitle and not test and name != '-' and not hasattr(name, 'write')
                and not self.params.get('test') and not info.get('is_live')
                and (info.get('extractor_key') == 'Youtube' or info.get('extractor') == 'youtube')
                and get_suitable_downloader(info, self.params, to_stdout=False) is HlsFD):
            new_info = self._copy_infodict(info)
            if new_info.get('http_headers') is None:
                new_info['http_headers'] = self._calc_headers(new_info)
            fd = YoutubeHlsFD(self, self.params)
            for hook in self._progress_hooks:
                fd.add_progress_hook(hook)
            return fd.download(name, new_info, subtitle)
        eligible = (self.params.get('_parallel_http') and not subtitle and not test
            and name != '-' and not hasattr(name, 'write')
            and self.params.get('concurrent_fragment_downloads', 1) > 1
            and not self.params.get('ratelimit') and not self.params.get('test')
            and not self.params.get('impersonate') and not info.get('impersonate')
            and not info.get('is_live') and not info.get('request_data')
            and info.get('section_start') is None and info.get('section_end') is None
            and (info.get('filesize') or 0) > int(self.params.get('http_chunk_size') or CHUNK_SIZE)
            and 'Range' not in HTTPHeaderDict(info.get('http_headers')))
        if not eligible or get_suitable_downloader(info, self.params, to_stdout=False) is not HttpFD:
            return super().dl(name, info, subtitle, test)
        new_info = self._copy_infodict(info)
        if new_info.get('http_headers') is None:
            new_info['http_headers'] = self._calc_headers(new_info)
        fd = ParallelHttpFD(self, self.params)
        for hook in self._progress_hooks:
            fd.add_progress_hook(hook)
        return fd.download(name, new_info, subtitle)
