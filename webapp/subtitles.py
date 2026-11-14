"""Best-effort sidecars in a separate, globally bounded subtitle lane."""
from pathlib import Path
import hashlib
import json
import math

import yt_dlp

from webapp.channel_scheduler import ChannelScheduler, throttle_details
from webapp.disk_budget import DiskBudget, MIB
from webapp.downloader import build_ydl_options, download_cookie_session
from webapp.downloads import DownloadLeaseLost
from webapp.subtitle_lane import check as check_subtitle_lane, slot as subtitle_slot, failed as subtitle_failed


from webapp.read_cache import ReadCache

_summaries = ReadCache(ttl=3)


def summary(db, stamp, identity):
    counts = _summaries.read((db.engine, identity), lambda: _counts(db, identity))
    # Only historical counts are cached; deadlines remain current.
    key = hashlib.sha256(b'subtitles:youtube-origin').hexdigest()
    from sqlalchemy import select
    from webapp.database import download_resources
    state = json.loads(db.execute(select(download_resources.c.state).where(download_resources.c.key == key)).scalar_one_or_none() or '{}')
    return {**counts, 'cooldown_seconds': max(0, math.ceil(state.get('cooldown_until', 0)-stamp)),
        'cooldown_status': state.get('cooldown_status') if state.get('cooldown_until', 0) > stamp else None,
        'interval_seconds': max(0, math.ceil(state.get('next_at', 0)-stamp))}


def _counts(db, identity):
    from sqlalchemy import func, or_, select
    from webapp.database import tasks
    values = {status: int(count) for status, count in db.execute(select(tasks.c.status, func.count()).where(
        tasks.c.action == 'subtitles', or_(tasks.c.target_instance_id == identity, tasks.c.instance_id == identity)
    ).group_by(tasks.c.status))}
    return values


def run_subtitles(store, payload, report):
    config = {**payload['config'], 'download_method': 'yt_dlp'}
    video = Path(payload['video_file']).resolve()
    if not video.is_file():
        return {'message': '视频文件已移除，字幕补下载已跳过。', 'skipped': True}
    if config.get('storage_mode') == 'test':
        return {'message': '测试模式不保留字幕。', 'skipped': True}
    scheduler = ChannelScheduler(store, config, payload.get('_task_id'), base=payload['base'])
    check_subtitle_lane(scheduler)
    if config.get('youtube_cookie_pool') and not payload.get('_subtitle_pool_admitted'):
        from webapp.cookie_pool import CookiePool
        pool = CookiePool(store, config, payload['base'], payload.get('_task_id'))
        # Sidecars must use the original video's account and share its cooling,
        # permits and start interval. A subtitle is not a successful video.
        with pool.assignment(config.get('_cookie_pool_selected'), count_success=False, rest_on_finish=False) as selected:
            return run_subtitles(store, {**payload, 'config': selected,
                '_subtitle_pool_admitted': True}, report)
    try:
        if scheduler.youtube_pacing:
            scheduler.wait_youtube_start(scheduler.channels[0])
        budget = DiskBudget(store.directory, config)
        with budget.lease('cache', task=payload.get('_task_id'), reserve=16 * MIB) as lease:
            config['_temporary_directory'] = str(lease.path)
            options = build_ydl_options(config, payload['base'])
            options.update(skip_download=True, download_archive=None, writesubtitles=True, writeautomaticsub=True,
                           writeinfojson=False, writethumbnail=False, ignoreerrors=False,
                           outtmpl=str(video.with_suffix('')).replace('%', '%%') + '.%(ext)s',
                           retries=0, fragment_retries=0, extractor_retries=1, socket_timeout=15, max_filesize=8 * MIB)
            def heartbeat(_):
                report({'stage': 'downloading_subtitles', 'speed': None, 'eta': None})
                lease.check()
            options['progress_hooks'] = [heartbeat]
            report({'stage': 'downloading_subtitles', 'speed': None, 'eta': None})
            if store and payload.get('_task_id') and not store.heartbeat(payload['_task_id'], store.owner):
                raise DownloadLeaseLost('字幕任务租约已失效。')
            with download_cookie_session(options) as session:
                class SubtitleDownloader(yt_dlp.YoutubeDL):
                    def dl(self, name, info, subtitle=False, test=False):
                        if subtitle:
                            with subtitle_slot(scheduler):
                                return super().dl(name, info, subtitle, test)
                        return super().dl(name, info, subtitle, test)
                with SubtitleDownloader(session) as downloader:
                    failed = downloader.download(payload['urls'])
                    if failed:
                        raise RuntimeError('字幕补下载失败，视频已保留。')
            if config.get('storage_mode') == 'oss':
                from Utils.oss_storage import OSSStorage
                from Utils.config import video_directory
                config['_disk_state'] = str(store.directory)
                def upload_progress(value):
                    lease.check()
                    report(value)
                objects = OSSStorage(config).upload_video(video, video_directory(config, payload['base']),
                                                         upload_progress, include_video=False)
                return {'message': '字幕补下载已完成，配套文件已同步到 OSS。', 'video_preserved': True,
                        'oss_bucket': config['oss_bucket'], 'oss_objects': objects}
            return {'message': '字幕补下载已完成；源视频没有所选语言时不生成字幕。', 'video_preserved': True}
    except Exception as error:
        from Utils.oss_storage import OSSUploadError
        if isinstance(error, OSSUploadError):
            raise  # OSS failures must not cool subtitle/YouTube resources.
        from webapp.downloads import YouTubeAccountLimited
        account_limit = (YouTubeAccountLimited(str(error))
                         if 'your account has been rate-limited' in str(error).lower() else None)
        failure = account_limit or error
        subtitle_failed(scheduler, failure)
        status, _ = throttle_details(failure)
        if account_limit or scheduler.youtube_pacing and status in (429, 503):
            scheduler.outcome(scheduler.channels[0], success=False, error=failure, phase='subtitles')
        if account_limit:
            raise account_limit from error
        raise
