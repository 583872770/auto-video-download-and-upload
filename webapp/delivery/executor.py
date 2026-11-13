"""Native delivery attempts share acquired tracks and retain verified outputs."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import shutil
import time
import uuid

from sqlalchemy import select
import yt_dlp
from Utils.config import video_directory
from webapp.downloader import build_ydl_options, download_cookie_session
from webapp.delivery.contracts import variant
from webapp.delivery import artifacts as ledger
from webapp.delivery.verify import verify, convert, inspect, LocalDeliveryError
from webapp.database import delivery_requirements as requirements, delivery_artifacts as artifacts
from webapp.parallel_http import ParallelYoutubeDL
from webapp.local_processing import local_processing


def reporting_downloader(session):
    class DeliveryDownloader(ParallelYoutubeDL):
        def report_warning(self, message, *args, **kwargs):
            session.failure.warning(message)
            session.progress.state['warning_counts'] = dict(session.failure.warnings)
            if session.failure.cookie_rejected:
                from webapp.youtube_auth import YouTubeAuth, YouTubeAuthRequired
                raise YouTubeAuthRequired(YouTubeAuth(session.store).reject(session.failure.message(),
                    node=session.config.get('_resolved_clash_node', ''),
                    profile_id=session.config.get('_cookie_pool_selected')))
            return super().report_warning(message, *args, **kwargs)

        def report_error(self, message, *args, **kwargs):
            session.failure.error(message)
            return super().report_error(message, *args, **kwargs)
    return DeliveryDownloader


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
    local_id = session.store.fleet.id if session.store.fleet else 'local'
    if artifact and not artifact['available'] and not artifact['test_mode'] and artifact['instance_id'] == local_id:
        path = Path(artifact['path'])
        if path.is_file():
            try:
                verify(path, kind)
            except LocalDeliveryError:
                return False
            publish(session, path, kind, language, retained=True)
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
            storage = OSSStorage(session.config)
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
                publish(session, Path(artifact['path']), kind, language, retained=True)
                return True
    return False


def publish(session, source, kind, language='', *, retained=False):
    spec = session.config['_delivery_contract']
    task_id = session.payload['_task_id']
    with session.store.connect() as db:
        task = ledger.fenced_run(session.store, db, task_id)
        identity = ledger.artifact_identity(task['batch_id'], task['video_id'], spec, kind, language, session.test_mode)
    root = source.parent if retained else video_directory(session.config, session.base)
    root.mkdir(parents=True, exist_ok=True)
    target = source if retained else root / (identity + '-' + uuid.uuid4().hex + source.suffix)
    verify(source, kind)
    # Attempt-local paths cannot overwrite a later worker's published output.
    if not retained:
        shutil.copyfile(source, target)
    artifact = dict(id=identity, kind=kind, language=language, variant=variant(spec, kind),
                    title=session.current_video.get('title', ''), storage='oss' if session.oss_mode else 'local')
    if not session.test_mode:
        ledger.pending(session.store, task_id, artifact, target)
    if session.oss_mode:
        from Utils.oss_storage import OSSStorage
        session.progress.state.update(stage='uploading_oss', output_kind=kind)
        session.progress.save(force=True)
        uploaded = OSSStorage(session.config).upload_files([target], root,
            lambda value: (session.progress.state.update(value), session.progress.save(force=True)))
        artifact['object_key'] = uploaded[0]['key']
    # Crash recovery carries the same semantic receipt as normal registration.
    if session.test_mode and session.payload.get('_disk_workspace'):
        receipt = Path(session.payload['_disk_workspace']) / '.completed-files.json'
        existing = json.loads(receipt.read_text()) if receipt.is_file() else []
        existing = [r for r in existing if r.get('artifact', {}).get('id') != identity]
        existing.append(dict(path=str(target), size=target.stat().st_size, complete=True, method='yt_dlp',
                             video_id=task['video_id'], title=artifact['title'], artifact=artifact))
        temporary = receipt.with_suffix('.tmp')
        with temporary.open('w', encoding='utf-8') as output:
            json.dump(existing, output);output.flush();os.fsync(output.fileno())
        temporary.replace(receipt)
    new = session.store.statistics.record_file(task_id, target, task['video_id'], artifact['title'],
        test_mode=session.test_mode, artifact=artifact)
    if new and not session.test_mode:
        session.billable_bytes += target.stat().st_size
    session.output_files[str(target)] = target.stat().st_size
    session.meter.verified(target.stat().st_size)


def choose(info, kind, max_height):
    formats = info.get('formats') or [info]
    video = kind == 'video_only'
    candidates = [f for f in formats if f.get('url') and (f.get('vcodec', 'none') != 'none' if video else f.get('acodec', 'none') != 'none')
                  and (not video or not max_height or (f.get('height') or 0) <= max_height)]
    only = [f for f in candidates if f.get('acodec' if video else 'vcodec', 'none') == 'none']
    if not candidates:
        return None
    return (only or candidates)[-1]


@contextmanager
def subtitle_slot(session):
    scheduler = session.scheduler
    if not scheduler.enabled:
        yield
        return
    from webapp.throughput import PipelineBusy
    scope = 'subtitles:youtube-origin'
    def admit(db, stamp):
        state = scheduler._state(db, scope)
        delay = max(state.get('next_at', 0), state.get('cooldown_until', 0)) - stamp
        if delay > 0:
            return None, delay
        token, delay = scheduler._admit(db, stamp, [(scope, 1, 1)])
        if token:
            state['next_at'] = stamp + 10
            scheduler._save(db, scope, state)
        return token, delay
    token, delay = scheduler.transaction_call(admit)
    if not token:
        error = PipelineBusy('字幕请求等待上游名额，已完成媒体文件保留。');error.retry_after = max(2, delay)
        raise error
    try:
        yield
    finally:
        scheduler._release(token)


def attempt(executor, channel, addresses, *, without_cookies=False, cookie_retry=False):
    session = executor.session
    from webapp.downloads import DownloadFailure
    session.failure = DownloadFailure()
    session.failure_phase = 'resolve'
    session.transfer_seconds = 0
    if not session.store or len(addresses) != 1:
        raise RuntimeError('多类型交付需要通过客户任务逐条执行。')
    if not shutil.which('ffmpeg') or not shutil.which('ffprobe'):
        from webapp.environment import DownloadEnvironmentRequired
        raise DownloadEnvironmentRequired('多类型交付需要 FFmpeg 和 FFprobe。')
    spec = session.config['_delivery_contract']
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
    from webapp.channels import execution_config
    from webapp.parallel_http import fragment_connection_budget, transfer_connection_units
    from webapp.throughput import runtime_limits
    transfer_count = runtime_limits(session.config, session.store.directory)['transfers']
    fragment_budget = fragment_connection_budget(session.config, channel.connection_limit, transfer_count)
    options = build_ydl_options({**execution_config(session.config, channel), '_use_youtube_cookie': not without_cookies}, session.base)
    options.update(ignoreerrors=False, download_archive=None, writesubtitles=False, writeautomaticsub=False,
                   writeinfojson=False, writethumbnail=False, post_hooks=[],
                   format='bestvideo/bestaudio/best', progress_hooks=[progress.hook, session.meter.hook],
                   retries=2, fragment_retries=2, extractor_retries=1, concurrent_fragment_downloads=fragment_budget)
    if without_cookies or session.config.get('youtube_cookie_usage') == 'never' and not session.config.get('_cookie_pool_selected'):
        options.pop('cookiefile', None)
    temporary = Path(session.config.get('_temporary_directory') or session.payload['_disk_workspace']) / 'delivery'
    temporary.mkdir(parents=True, exist_ok=True)
    progress.state.update(download_method='yt_dlp', channel_id=channel.id, output_types=spec['outputs'], delivery_version=session.config['_delivery_version'])
    session.meter.begin(channel)
    try:
        with download_cookie_session(options) as safe:
            with reporting_downloader(session)(safe) as downloader:
                if session.youtube:
                    session.scheduler.wait_youtube_start(channel)
                with session.scheduler.permit(channel, 'resolve'):
                    progress.state.update(stage='extracting');progress.save(force=True)
                    started = time.monotonic()
                    info = downloader.extract_info(addresses[0], download=False)
                    session.meter.resolved(time.monotonic() - started)
                session.scheduler.prepared()
                if not info or info.get('_type') in ('playlist', 'multi_video'):
                    raise RuntimeError('来源不是可处理的单条媒体。')
                session.current_video.update(id=info.get('id'), title=info.get('title', ''))
                progress.state['title'] = info.get('title', '')
                tracks = {}
                acquired = {}
                need_video = any(k in remaining for k in ('video_only', 'av'))
                need_audio = any(k in remaining for k in ('audio_only', 'av'))
                for kind, needed in (('video_only', need_video), ('audio_only', need_audio)):
                    if not needed:
                        continue
                    selected = choose(info, kind, spec['video']['max_height'])
                    if not selected:
                        tracks[kind] = None
                        continue
                    source_key = (selected.get('format_id'), selected['url'])
                    if source_key in acquired:
                        tracks[kind] = acquired[source_key]
                        continue
                    ext = selected.get('ext', 'mkv')
                    if not re.fullmatch(r'[a-zA-Z0-9]{1,8}', ext):
                        raise RuntimeError('来源格式扩展名无效。')
                    path = temporary / ('source-' + kind + '.' + ext)
                    progress.state.update(output_kind=kind, stage='downloading');progress.save(force=True)
                    session.failure_phase = 'transfer'
                    transfer_info = {**info, **selected}
                    with executor.transmitting(channel, transfer_info, transfer_connection_units(transfer_info, fragment_budget, native=True)):
                        success, _ = downloader.dl(str(path), transfer_info)
                    if not success:
                        raise RuntimeError('来源媒体轨道下载失败。')
                    tracks[kind] = path
                    acquired[source_key] = path
                for kind in remaining:
                    inputs = [tracks.get('video_only'), tracks.get('audio_only')] if kind == 'av' else [tracks.get(kind)]
                    if not all(inputs):
                        ledger.mark(session.store, task_id, keys[kind], 'missing', '来源没有符合要求的画面或音轨。')
                        continue
                    mp4 = spec['av']['container'] == 'mp4' if kind == 'av' else kind == 'video_only' and spec['video']['container'] == 'mp4'
                    mp3 = kind == 'audio_only' and spec['audio']['format'] == 'mp3'
                    auto_mp4 = False
                    if kind == 'av' and spec['av']['container'] == 'auto':
                        _, video_info = inspect(inputs[0]);_, audio_info = inspect(inputs[1])
                        video_codec = next(s['codec_name'] for s in video_info['streams'] if s.get('codec_type') == 'video')
                        audio_codec = next(s['codec_name'] for s in audio_info['streams'] if s.get('codec_type') == 'audio')
                        auto_mp4 = video_codec in ('h264', 'hevc', 'av1', 'mpeg4') and audio_codec in ('aac', 'alac', 'mp3')
                    ext = '.mp3' if mp3 else '.mp4' if mp4 or auto_mp4 else '.mkv' if kind == 'av' else inputs[0].suffix
                    if kind == 'audio_only' and not mp3:
                        types, media = inspect(inputs[0])
                        audio = next(s for s in media['streams'] if s.get('codec_type') == 'audio')
                        ext = '.m4a' if audio.get('codec_name') in ('aac', 'alac') else '.webm' if audio.get('codec_name') in ('opus', 'vorbis') else '.mka'
                    output = temporary / (kind + ext)
                    progress.state.update(stage='postprocessing', output_kind=kind);progress.save(force=True)
                    with local_processing(session.payload.get('_disk_state') or session.store.directory, session.config, progress, 'delivery_' + kind):
                        convert(inputs, output, kind, mp4=mp4, mp3=mp3)
                    publish(session, output, kind)
                if not sub_done:
                    subtitles(session, info, safe, temporary)
    except yt_dlp.utils.DownloadError as error:
        text = session.failure.message(error).lower()
        if without_cookies and not cookie_retry and not session.config.get('_cookie_pool_selected') and session.config.get('youtube_cookie_usage', 'auto') == 'auto' and session.config.get('youtube_cookie') and not any(s in text for s in ('bot', 'captcha')) and any(s in text for s in ('sign in to', 'login required', 'age-restricted', 'private video')):
            return attempt(executor, channel, addresses, without_cookies=False, cookie_retry=True)
        raise session.failure.as_error(error) from error
    return False


def subtitles(session, info, safe, temporary):
    spec = session.config['_delivery_contract']
    config = spec['subtitles']
    task_id = session.payload['_task_id']
    available = {}
    for language in config['languages']:
        manual = info.get('subtitles', {}).get(language) if 'manual' in config['sources'] else None
        automatic = info.get('automatic_captions', {}).get(language) if 'automatic' in config['sources'] else None
        if manual or automatic:
            available[language] = (manual, automatic)
    with session.store.connect() as db:
        ledger.fenced_run(session.store, db, task_id)
        # Replace the initial group requirement only after successful extraction.
        if available or config['language_policy'] == 'all_selected':
            db.execute(requirements.delete().where(requirements.c.task_id == task_id,
                requirements.c.kind == 'subtitle', requirements.c.language == '', requirements.c.status != 'waived'))
            for language in config['languages'] if config['language_policy'] == 'all_selected' else available:
                ledger.requirement(session.store, db, task_id, spec, 'subtitle', language)
    if not available:
        languages = config['languages'] if config['language_policy'] == 'all_selected' else ['']
        for language in languages:
            key = require_key(session, 'subtitle', language)
            ledger.mark(session.store, task_id, key, 'missing', '来源没有所需语言的字幕。')
        return
    for language in config['languages'] if config['language_policy'] == 'all_selected' else available:
        if ready(session, 'subtitle', language):
            continue
        key = require_key(session, 'subtitle', language)
        if language not in available:
            ledger.mark(session.store, task_id, key, 'missing', '来源没有该语言字幕。')
            continue
        directory = temporary / language
        directory.mkdir(exist_ok=True)
        options = {**safe, 'skip_download': True, 'writesubtitles': True, 'writeautomaticsub': True,
                   'subtitleslangs': [language], 'subtitlesformat': 'vtt/best',
                   'outtmpl': str(directory / 'caption.%(ext)s'), 'progress_hooks': [],
                   'post_hooks': [], 'postprocessors': [], 'max_filesize': 8 * 1024 * 1024}
        manual, automatic = available[language]
        subtitle_info = {**info, 'subtitles': {language: manual} if manual else {},
                         'automatic_captions': {language: automatic} if not manual and automatic else {}}
        session.progress.state.update(stage='downloading_subtitles', output_kind='subtitle', output_language=language)
        session.progress.save(force=True)
        try:
            with subtitle_slot(session):
                with reporting_downloader(session)(options) as downloader:
                    downloader.process_ie_result(subtitle_info, download=True)
            files = [p for p in directory.iterdir() if p.suffix in ('.vtt', '.srt', '.ttml', '.srv3', '.json3')]
            if not files:
                raise RuntimeError('字幕请求未生成可用文件。')
            source = files[0]
            target = directory / ('delivery.' + config['format'])
            with local_processing(session.payload.get('_disk_state') or session.store.directory, session.config,
                                  session.progress, 'delivery_subtitle'):
                if source.suffix == target.suffix:
                    shutil.copyfile(source, target);verify(target, 'subtitle')
                else:
                    convert([source], target, 'subtitle')
            publish(session, target, 'subtitle', language)
        except Exception as error:
            from webapp.throughput import PipelineBusy
            if not isinstance(error, PipelineBusy):
                ledger.mark(session.store, task_id, key, 'failed', '字幕获取或交付失败，可重试补交。')
            raise


def finish(session):
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
    if status not in ('complete', 'accepted'):
        return None
    return dict(message='已恢复已校验的测试产物。', test_mode=True, skipped=False,
                file_count=len(receipts), file_bytes=sum(r['size'] for r in receipts),
                billable_bytes=0, download_method='yt_dlp', recovered=True, delivery_status=status)
