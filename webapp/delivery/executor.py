"""Native delivery attempts share acquired tracks and retain verified outputs."""
import json
import os
from pathlib import Path
import re
import shutil
import time
import uuid
from contextlib import contextmanager

from sqlalchemy import select
import yt_dlp
from yt_dlp.networking.exceptions import HTTPError
from Utils.config import video_directory
from webapp.downloader import build_ydl_options, download_cookie_session
from webapp.delivery.contracts import variant
from webapp.delivery import artifacts as ledger
from webapp.delivery.verify import verify, LocalDeliveryError
from webapp.database import delivery_requirements as requirements, delivery_artifacts as artifacts
from webapp.parallel_http import ParallelYoutubeDL
from webapp.subtitle_tracks import SubtitleSession, caption_plan, translated, provider_options


class DeliveryDownloader(ParallelYoutubeDL):
    def __init__(self, session, options):
        self.session = session
        self.observation = None
        super().__init__(options)

    def close(self):
        try:
            super().close()
        finally:
            self.session = None

    def to_screen(self, message, *args, **kwargs):
        if self.observation and str(message).startswith('[download] Sleeping '):
            sleeping = re.match(r'\[download\] Sleeping (\d+(?:\.\d+)?) seconds\b', str(message))
            self.observation.sleeping(float(sleeping[1]) if sleeping else None)
        return super().to_screen(message, *args, **kwargs)

    @contextmanager
    def file_observation(self, fd, name):
        from webapp.transfer_observation import FileTransferObservation
        observation = self.observation = FileTransferObservation(self.session, fd, name)
        try:
            yield
        except BaseException:
            # Preserve the original HTTP/lease/space error and its cooldown evidence.
            observation.close(publish=False)
            raise
        finally:
            try:
                observation.close()
            finally:
                observation.detach()
                self.observation = None

    def report_warning(self, message, *args, **kwargs):
        session = self.session
        session.failure.warning(message)
        session.progress.state['warning_counts'] = dict(session.failure.warnings)
        if session.failure.cookie_rejected:
            from webapp.youtube_auth import YouTubeAuth, YouTubeAuthRequired
            raise YouTubeAuthRequired(YouTubeAuth(session.store).reject(session.failure.message(),
                node=session.config.get('_resolved_clash_node', ''),
                profile_id=session.config.get('_cookie_pool_selected')))
        return super().report_warning(message, *args, **kwargs)

    def report_error(self, message, *args, **kwargs):
        session = self.session
        session.failure.error(message)
        return super().report_error(message, *args, **kwargs)

def require_key(session, kind, language=''):
    with session.store.connect() as db:
        ledger.fenced_run(session.store, db, session.payload['_task_id'])
        return ledger.requirement(session.store, db, session.payload['_task_id'], session.config['_delivery_contract'], kind, language)


def ready(session, kind, language=''):
    key = require_key(session, kind, language)
    with session.store.connect() as db:
        row = db.execute(select(requirements).where(requirements.c.task_id == session.payload['_task_id'], requirements.c.key == key)).mappings().one()
        task = ledger.fenced_run(session.store, db, session.payload['_task_id'])
        identity = ledger.artifact_identity(task['batch_id'], task['video_id'], session.config['_delivery_contract'], kind, language, session.test_mode)
        artifact = db.execute(select(artifacts).where(artifacts.c.id == (row['artifact_id'] or identity))).mappings().first()
    if row['status'] == 'waived':
        return True
    if artifact:
        session.current_video.update(title=artifact['title'])
    # 留样重试与立即停止后重启都要求取一份本次执行的测试文件。
    fresh_copy = session.payload.get('_force_test_sample') or session.payload.get('_redownload_after_stop')
    same_workspace = artifact and Path(artifact['path']).is_relative_to(Path(session.payload.get('_disk_workspace') or '/__none__'))
    if session.test_mode and artifact and artifact['test_mode'] and (not fresh_copy or same_workspace):
        with session.store.connect() as db:
            ledger.fenced_run(session.store, db, task['id'])
            recorded = ledger.recorded_test_output(db, task, artifact)
            if recorded:
                db.execute(requirements.update().where(requirements.c.task_id == task['id'],
                    requirements.c.key == key).values(status='done', artifact_id=artifact['id'], error=None))
                ledger.refresh(session.store, db, task['id'])
        if recorded:
            session.output_files[recorded['path']] = recorded['size']
            return True
    local_id = session.store.fleet.id if session.store.fleet else 'local'
    if artifact and not artifact['available'] and not artifact['test_mode'] and artifact['instance_id'] == local_id:
        path = Path(artifact['path'])
        if path.is_file():
            try:
                verify(path, kind)
            except LocalDeliveryError:
                return False
            publish(session, path, kind, language, retained=True, subtitle_source=artifact['subtitle_source'])
            return True
    if artifact and artifact['available'] and not artifact['test_mode']:
        local = artifact['instance_id'] == (session.store.fleet.id if session.store.fleet else 'local')
        if local and artifact['storage'] != 'oss' and Path(artifact['path']).is_file():
            try:
                verify(artifact['path'], kind)
            except LocalDeliveryError:
                return False
            with session.store.connect() as db:
                ledger.fenced_run(session.store, db, session.payload['_task_id'])
                db.execute(requirements.update().where(requirements.c.task_id == session.payload['_task_id'], requirements.c.key == key).values(status='done', artifact_id=artifact['id'], error=None))
                ledger.refresh(session.store, db, session.payload['_task_id'])
            session.output_files[artifact['path']] = artifact['size']
            return True
        if artifact['storage'] == 'oss':
            from Utils.oss_storage import OSSStorage
            with session.store.connect() as db:
                storage = OSSStorage(ledger.storage_config(db, artifact))
            try:
                exists = storage.bucket.head_object(artifact['object_key']).content_length == artifact['size']
            except Exception:
                exists = False
            if exists:
                with session.store.connect() as db:
                    ledger.fenced_run(session.store, db, session.payload['_task_id'])
                    db.execute(requirements.update().where(requirements.c.task_id == session.payload['_task_id'], requirements.c.key == key).values(status='done', artifact_id=artifact['id'], error=None))
                    ledger.refresh(session.store, db, session.payload['_task_id'])
                session.output_files[artifact['path']] = artifact['size']
                return True
            if local and Path(artifact['path']).is_file():
                try:
                    verify(artifact['path'], kind)
                except LocalDeliveryError:
                    return False
                publish(session, Path(artifact['path']), kind, language, retained=True,
                        subtitle_source=artifact['subtitle_source'])
                return True
    return False


def commit_publications(session, items):
    """One recovery manifest and one ledger transaction per bounded batch."""
    task_id = session.payload['_task_id']
    if session.test_mode and session.payload.get('_disk_workspace'):
        receipt = Path(session.payload['_disk_workspace']) / '.completed-files.json'
        existing = json.loads(receipt.read_text()) if receipt.is_file() else []
        identities = {item['artifact']['id'] for item in items}
        existing = [r for r in existing if r.get('artifact', {}).get('id') not in identities]
        existing.extend(dict(**item, complete=True, method='yt_dlp') for item in items)
        temporary = receipt.with_suffix('.tmp')
        with temporary.open('w', encoding='utf-8') as output:
            json.dump(existing, output);output.flush();os.fsync(output.fileno())
        temporary.replace(receipt)
    if len(items) == 1:
        item = items[0]
        recorded = [session.store.statistics.record_file(task_id, item['path'], item['video_id'],
            item['title'], test_mode=session.test_mode, artifact=item['artifact'])]
    else:
        recorded = session.store.statistics.record_files(task_id, items, test_mode=session.test_mode)
    for item, fresh in zip(items, recorded):
        if fresh and not session.test_mode:
            session.billable_bytes += item['size']
        session.output_files[item['path']] = item['size']
        session.meter.verified(item['size'])


def publish(session, source, kind, language='', *, retained=False, subtitle_source='', verified=False, destination=None, batch=None):
    spec = session.config['_delivery_contract']
    task_id = session.payload['_task_id']
    with session.store.connect() as db:
        task = ledger.fenced_run(session.store, db, task_id)
        identity = ledger.artifact_identity(task['batch_id'], task['video_id'], spec, kind, language, session.test_mode)
    subdirectory = Path()
    if session.config.get('shard_downloads'):
        video_id = task.get('video_id') or ''
        if not re.fullmatch(r'[A-Za-z0-9_-]{11}', video_id):
            raise LocalDeliveryError('视频 ID 无效，无法按视频 ID 保存交付文件。')
        subdirectory = Path(video_id)
    root = source.parent if retained else video_directory(session.config, session.base) / subdirectory
    root.mkdir(parents=True, exist_ok=True)
    target = source if retained else Path(destination) if destination else root / (identity + '-' + uuid.uuid4().hex + source.suffix)
    if not verified:
        verify(source, kind)
    # Attempt-local paths cannot overwrite a later worker's published output.
    if not retained:
        from webapp.delivery.files import move_output
        move_output(source, target)
    artifact = dict(id=identity, kind=kind, language=language, variant=variant(spec, kind),
                    subtitle_source=subtitle_source,
                    title=session.current_video.get('title', ''), storage='oss' if session.oss_mode else 'local')
    if not session.test_mode:
        ledger.pending(session.store, task_id, artifact, target)
    if session.oss_mode:
        from Utils.oss_storage import OSSStorage
        # upload_files uses root-relative names. Keep the same video directory
        # in the object key even when reusing a file from a previous attempt.
        storage_config = session.config
        if subdirectory.parts:
            storage_config = {**storage_config, '_oss_namespace': '/'.join(filter(None,
                (storage_config.get('_oss_namespace', ''), subdirectory.as_posix())))}
        session.progress.state.update(stage='uploading_oss', output_kind=kind)
        session.progress.save(force=True)
        uploaded = OSSStorage(storage_config).upload_files([target], root,
            lambda value: (session.progress.state.update(value), session.progress.save(force=True)))
        artifact['object_key'] = uploaded[0]['key']
    item = dict(path=str(target), size=target.stat().st_size, video_id=task['video_id'],
                title=artifact['title'], artifact=artifact)
    if batch is None:
        commit_publications(session, [item])
    else:
        batch.append(item)


def choose(info, kind, max_height):
    formats = info.get('formats') or [info]
    video = kind == 'video_only'
    candidates = [f for f in formats if f.get('url') and (f.get('vcodec', 'none') != 'none' if video else f.get('acodec', 'none') != 'none')
                  and (not video or not max_height or (f.get('height') or 0) <= max_height)]
    only = [f for f in candidates if f.get('acodec' if video else 'vcodec', 'none') == 'none']
    if not candidates:
        return None
    return (only or candidates)[-1]


def attempt(executor, addresses):
    try:
        return acquire(executor, addresses)
    except Exception as error:
        from webapp.disk_budget import DiskPressure, is_disk_write_pressure
        monitor_error = executor.session.payload.get('_disk_monitor_error')
        # 磁盘受限时，原下载进程会恢复执行。把未完成的下载交给本地队列会造成重复归属。
        if isinstance(error, DiskPressure) or is_disk_write_pressure(error) or (monitor_error and monitor_error()):
            raise
        # Retain already acquired outputs even if a later caption request fails.
        # Settlement persists the source recovery decision before handing off
        # local work. Any permitted retry fetches only missing requirements.
        if executor.session.local_jobs:
            from webapp.delivery.processing import enqueue
            enqueue(executor.session, resume_network=True)
        raise


def acquire(executor, addresses):
    session = executor.session
    from webapp.downloads import DownloadFailure
    session.failure = DownloadFailure()
    session.failure_phase = 'resolve'
    session.transfer_seconds = 0
    if not session.store or len(addresses) != 1:
        raise RuntimeError('多类型交付需要通过客户任务逐条执行。')
    spec = session.config['_delivery_contract']
    if set(spec['outputs']) - {'thumbnail', 'metadata'} and (not shutil.which('ffmpeg') or not shutil.which('ffprobe')):
        from webapp.environment import DownloadEnvironmentRequired
        raise DownloadEnvironmentRequired('多类型交付需要 FFmpeg 和 FFprobe。')
    progress = session.progress
    task_id = session.payload['_task_id']
    keys = {kind: require_key(session, kind) for kind in spec['outputs'] if kind != 'subtitle'}
    # Register every requirement before any file can make this run complete.
    if 'subtitle' in spec['outputs']:
        with session.store.connect() as db:
            sub_exists = db.execute(select(requirements.c.key).where(requirements.c.task_id == task_id, requirements.c.kind == 'subtitle').limit(1)).first()
        if not sub_exists:
            require_key(session, 'subtitle')
    remaining = [kind for kind in spec['outputs'] if kind != 'subtitle' and not ready(session, kind)]
    sub_done = 'subtitle' not in spec['outputs']
    if not sub_done:
        with session.store.connect() as db:
            sub_rows = list(db.execute(select(requirements).where(requirements.c.task_id == task_id, requirements.c.kind == 'subtitle')).mappings())
        sub_done = bool(sub_rows) and all(r['status'] == 'waived' or r['language'] and ready(session, 'subtitle', r['language']) for r in sub_rows)
    if not remaining and sub_done:
        return False
    from webapp.concurrency_policy import fragment_limit
    fragment_budget = fragment_limit(session.config)
    options = build_ydl_options(session.config, session.base)
    options['_hls_buffer_directory'] = session.store.directory
    options['_hls_report_spill'] = session.meter.hls_spill
    options.update(ignoreerrors=False, writesubtitles=False, writeautomaticsub=False,
                   writeinfojson=False, writethumbnail=False, post_hooks=[],
                   format='bestvideo/bestaudio/best', progress_hooks=[progress.hook, session.meter.hook],
                   retries=2, fragment_retries=2, extractor_retries=1, concurrent_fragment_downloads=fragment_budget)
    if not sub_done:
        translating = spec['subtitles']['mode'] != 'original'
        options = provider_options(options, force_subs=translating)
        # Listing translated variants of author-provided captions is opt-in.
        options['writeautomaticsub'] = spec['subtitles'].get('mode') == 'original_and_translated'
    temporary = Path(session.config.get('_temporary_directory') or session.payload['_disk_workspace']) / 'delivery'
    progress.state.update(download_method='yt_dlp', output_types=spec['outputs'], delivery_version=session.config['_delivery_version'])
    session.meter.begin()
    try:
        with download_cookie_session(options) as safe:
            with DeliveryDownloader(session, safe) as downloader:
                with session.scheduler.permit('resolve'):
                    progress.state.update(stage='extracting');progress.save(force=True)
                    started = time.monotonic()
                    info = downloader.extract_info(addresses[0], download=False)
                    session.meter.resolved(time.monotonic() - started)
                caption_session = SubtitleSession(downloader)
                if not info or info.get('_type') in ('playlist', 'multi_video'):
                    raise RuntimeError('来源不是可处理的单条媒体。')
                # Build one acquisition/processing plan before any large write.
                # Signed URLs remain in memory; only byte estimates and local
                # file dependencies are handed to durable processing workers.
                from webapp.delivery.space import source_bytes, acquisition_peak
                tracks, acquired, sources, media_jobs = {}, {}, [], []
                for kind, needed in (('video_only', any(k in remaining for k in ('video_only', 'av'))),
                                     ('audio_only', any(k in remaining for k in ('audio_only', 'av')))):
                    if not needed:
                        continue
                    selected = choose(info, kind, spec['video']['max_height'])
                    if not selected:
                        tracks[kind] = None
                        continue
                    source_key = (selected.get('format_id'), selected['url'])
                    if source_key not in acquired:
                        ext = selected.get('ext', 'mkv')
                        if not re.fullmatch(r'[a-zA-Z0-9]{1,8}', ext):
                            raise RuntimeError('来源格式扩展名无效。')
                        path = temporary / ('source-' + kind + '.' + ext)
                        acquired[source_key] = path
                        sources.append(dict(kind=kind, path=path, format=selected,
                                            bytes=source_bytes(selected, info.get('duration'))))
                    tracks[kind] = acquired[source_key]
                for kind in sorted(remaining, key=lambda kind: kind != 'av'):
                    if kind in ('thumbnail', 'metadata'):
                        continue
                    inputs = [tracks.get('video_only'), tracks.get('audio_only')] if kind == 'av' else [tracks.get(kind)]
                    if not all(inputs):
                        ledger.mark(session.store, task_id, keys[kind], 'missing', '来源没有符合要求的画面或音轨。')
                        continue
                    media_jobs.append(dict(kind=kind, inputs=[str(p) for p in inputs],
                        mp4=spec['av']['container'] == 'mp4' if kind == 'av' else kind == 'video_only' and spec['video']['container'] == 'mp4',
                        mp3=kind == 'audio_only' and spec['audio']['format'] == 'mp3',
                        auto=kind == 'av' and spec['av']['container'] == 'auto'))
                forecast = acquisition_peak(sources, media_jobs, info.get('duration'), test_mode=session.test_mode)
                if not sub_done:
                    forecast['peak_bytes'] += 24 * 1024**2
                progress.state['space_plan'] = forecast
                progress.save(force=True)
                # 完成空间预留后再创建写入目录并获取交付文件。
                temporary.mkdir(parents=True, exist_ok=True)
                session.current_video.update(id=info.get('id'), title=info.get('title', ''))
                progress.state['title'] = info.get('title', '')
                if 'metadata' in remaining:
                    from webapp.delivery.metadata import video_metadata
                    progress.state.update(stage='processing', output_kind='metadata');progress.save(force=True)
                    output = temporary / 'video.info.json'
                    output.write_text(json.dumps(video_metadata(info), ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')
                    publish(session, output, 'metadata')
                    print('视频信息已保存：JSON，包含标题、简介、频道等来源资料。', flush=True)
                downloaded = set()
                for source in sources:
                    kind, selected, path = source['kind'], source['format'], source['path']
                    progress.state.update(output_kind=kind, stage='downloading');progress.save(force=True)
                    session.failure_phase = 'transfer'
                    transfer_info = {**info, **selected}
                    with executor.transmitting():
                        success, _ = downloader.dl(str(path), transfer_info)
                    if not success:
                        raise RuntimeError('来源媒体轨道下载失败。')
                    downloaded.add(str(path))
                    session.local_jobs = [job for job in media_jobs if set(job['inputs']) <= downloaded]
                    # Refine the extraction estimate at a file boundary, before
                    # long multi-language caption waits retain unused capacity.
                    source['bytes'] = path.stat().st_size
                    source['format'] = {**selected, 'filesize': source['bytes']}
                    forecast = acquisition_peak(sources, media_jobs, info.get('duration'), test_mode=session.test_mode)
                    if not sub_done:
                        forecast['peak_bytes'] += 24 * 1024**2
                    progress.state['space_plan'] = forecast
                    progress.save(force=True)
                # 先完成媒体传输，封面请求等待期间仍可在失败分支保全已下载轨道。
                if 'thumbnail' in remaining:
                    thumbnail(executor, info, downloader, temporary, keys['thumbnail'])
                # Acquisition ends here. Conversion, verification and large-file
                # publication run in independent local workers, without Cookies.
                if not sub_done:
                    subtitles(executor, info, temporary, caption_session)
    except (yt_dlp.utils.DownloadError, HTTPError) as error:
        raise session.failure.as_error(error) from error
    return False


def thumbnail(executor, info, downloader, temporary, key):
    from PIL import Image
    from webapp.delivery.thumbnails import candidates, MissingThumbnails
    session = executor.session
    strategy = session.config['_delivery_contract']['thumbnail']['strategy']
    urls = candidates(info, strategy)
    missing = MissingThumbnails(session.store.directory, session.payload['_task_id'])
    known_missing = missing.read()
    urls = [url for url in urls if missing.key(url) not in known_missing]
    if not urls:
        ledger.mark(session.store, session.payload['_task_id'], key, 'missing', '来源没有可用封面图。')
        return
    source = temporary / 'thumbnail.image'
    transfer_info = dict(id=info['id'], title=info.get('title', ''), url=urls[0],
                         ext='jpg', protocol='https' if urls[0].startswith('https:') else 'http',
                         http_headers=info.get('http_headers') or {}, format_id='thumbnail')
    session.progress.state.update(output_kind='thumbnail', output_language='', stage='downloading')
    session.progress.save(force=True)
    with executor.transmitting():
        for url in urls:
            transfer_info.update(url=url, protocol='https' if url.startswith('https:') else 'http')
            try:
                success, _ = downloader.dl(str(source), transfer_info)
            except (yt_dlp.utils.DownloadError, HTTPError) as error:
                if not re.search(r'HTTP Error (404|410)\b', str(error)):
                    raise
                # Generated high-resolution URLs can be absent. Try the next
                # source image; authentication/rate-limit failures still escape.
                from webapp.downloads import DownloadFailure
                session.failure = DownloadFailure()
                missing.record(url)
                if isinstance(error, HTTPError):
                    error.response.close()
                print('该规格封面不存在，尝试来源提供的下一张封面。', flush=True)
                continue
            if not success:
                raise RuntimeError('封面图下载失败。')
            break
        else:
            ledger.mark(session.store, session.payload['_task_id'], key, 'missing', '来源提供的封面均已失效。')
            return
    verify(source, 'thumbnail')
    with Image.open(source) as image:
        extension = {'JPEG': '.jpg', 'PNG': '.png', 'WEBP': '.webp'}[image.format]
    target = source.with_suffix(extension)
    source.replace(target)
    publish(session, target, 'thumbnail')
    print('封面图已保存：' + ('最佳画质优先' if strategy == 'best' else '来源明确提供的封面，减少无效探测') + '，保留原始图片格式。', flush=True)


def subtitles(executor, info, temporary, caption_session):
    session = executor.session
    spec = session.config['_delivery_contract']
    config = spec['subtitles']
    task_id = session.payload['_task_id']
    available, requested = caption_plan(info, config)
    with session.store.connect() as db:
        ledger.fenced_run(session.store, db, task_id)
        # Replace the initial group requirement only after successful extraction.
        if requested:
            db.execute(requirements.delete().where(requirements.c.task_id == task_id,
                requirements.c.kind == 'subtitle', requirements.c.language == '', requirements.c.status != 'waived'))
            # Availability may change between retries. Retain receipts and explicit
            # waivers, but discard unresolved languages no longer in this plan.
            if config.get('mode') in ('original', 'original_and_translated'):
                db.execute(requirements.delete().where(requirements.c.task_id == task_id,
                    requirements.c.kind == 'subtitle', requirements.c.language.not_in(requested),
                    requirements.c.status.in_(('pending', 'failed', 'missing'))))
            for language in requested:
                ledger.requirement(session.store, db, task_id, spec, 'subtitle', language)
    if not available:
        languages = requested or ['']
        for language in languages:
            key = require_key(session, 'subtitle', language)
            reason = ('来源未明确视频主语言，无法确认应交付的主语言字幕；请指定语言或选择全部原始语言。'
                      if config['scope'] == 'main' and not language else '来源没有所需语言的字幕。')
            ledger.mark(session.store, task_id, key, 'missing', reason)
        return
    for language in requested:
        if ready(session, 'subtitle', language):
            continue
        key = require_key(session, 'subtitle', language)
        if language not in available:
            ledger.mark(session.store, task_id, key, 'missing',
                '来源未明确视频主语言，请指定语言或选择全部原始语言。'
                if config['scope'] == 'main' and not language else '来源没有该语言字幕。')
            continue
        directory = temporary / language
        directory.mkdir(exist_ok=True)
        manual, automatic = available[language]
        from webapp.diagnostics import event
        source_kind = 'translated' if any(translated(track) for track in (manual or automatic)) else 'manual' if manual else 'original_auto'
        event('subtitle_selected', language=language, source=source_kind)
        session.progress.state.update(output_kind='subtitle', output_language=language,
                                      subtitle_source=source_kind)
        session.progress.save(force=True)
        try:
            with executor.transmitting():
                written = caption_session.download(info, language, (manual, automatic), directory)
            files = [Path(p) for p in written if Path(p).is_file()
                     and Path(p).suffix in ('.vtt', '.srt', '.ttml', '.srv3', '.json3')]
            if not files:
                raise RuntimeError('字幕请求未生成可用文件。')
            source = files[0]
            session.local_jobs.append(dict(kind='subtitle', inputs=[str(source)], language=language,
                format=config['format'], subtitle_source=source_kind))
        except Exception as error:
            from webapp.throughput import PipelineBusy
            if not isinstance(error, PipelineBusy):
                ledger.mark(session.store, task_id, key, 'failed', '字幕获取或交付失败，可重试补交。')
            raise


def finish(session):
    if session.local_jobs:
        from webapp.delivery.processing import enqueue
        return enqueue(session)
    with session.store.connect() as db:
        ledger.fenced_run(session.store, db, session.payload['_task_id'])
        status = ledger.refresh(session.store, db, session.payload['_task_id'])
    session.progress.state.update(delivery_status=status, stage='completed')
    session.progress.save(force=True)
    return dict(message='交付完成。' if status == 'complete' else '已按约定结束。' if status == 'accepted' else '部分内容缺失，请在任务详情确认或补交。',
                delivery_status=status, file_count=len(session.output_files), file_bytes=sum(session.output_files.values()),
                billable_bytes=session.billable_bytes, skipped=False, download_method='yt_dlp',
                storage_mode=session.config.get('storage_mode', 'local'), fallback_used=False)


def recover_test_outputs(store, task_id, receipts, root):
    """Register typed crash receipts without mistaking a partial run for completion."""
    from webapp.disk_budget import safe_path
    for item in receipts:
        if not item.get('artifact') or not item.get('complete'):
            return None
        with store.connect() as db:
            task = ledger.fenced_run(store, db, task_id)
            if ledger.recorded_test_output(db, task, item['artifact']):
                continue  # Never overwrite a newer artifact from an old workspace.
        path = safe_path(item['path'], root)
        if not path.is_file() or path.stat().st_size != item['size']:
            return None
        verify(path, item['artifact']['kind'])
        if not store.heartbeat(task_id, store.owner):
            return None
        store.statistics.record_file(task_id, path, item.get('video_id', ''), item.get('title', ''),
                                     test_mode=True, artifact=item['artifact'])
    with store.connect() as db:
        status = ledger.refresh(store, db, task_id)
        from sqlalchemy import func
        from webapp.database import test_files
        count, size = db.execute(select(func.count(), func.coalesce(func.sum(test_files.c.size), 0))
            .select_from(test_files.join(requirements, requirements.c.artifact_id == test_files.c.id))
            .where(test_files.c.task_id == task_id, requirements.c.task_id == task_id,
                   requirements.c.status == 'done')).one()
    if status not in ('complete', 'accepted'):
        return None
    return dict(message='已恢复已校验的测试产物。', test_mode=True, skipped=False,
                file_count=count, file_bytes=size,
                billable_bytes=0, download_method='yt_dlp', recovered=True, delivery_status=status)
