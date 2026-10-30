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
from webapp.throughput import PipelineBusy


def summary(db, stamp, identity):
    from sqlalchemy import func, or_, select
    from webapp.database import tasks
    values = {status: int(count) for status, count in db.execute(select(tasks.c.status, func.count()).where(
        tasks.c.action == 'subtitles', or_(tasks.c.target_instance_id == identity, tasks.c.instance_id == identity)
    ).group_by(tasks.c.status))}
    key = hashlib.sha256(b'subtitles:youtube-origin').hexdigest()
    from webapp.database import download_resources
    state = json.loads(db.execute(select(download_resources.c.state).where(download_resources.c.key == key)).scalar_one_or_none() or '{}')
    values['cooldown_seconds'] = max(0, math.ceil(state.get('cooldown_until', 0)-stamp))
    return values


def run_subtitles(store, payload, report):
    config = {**payload['config'], 'download_method': 'yt_dlp'}
    video = Path(payload['video_file']).resolve()
    if not video.is_file():
        return {'message': '视频文件已移除，字幕补下载已跳过。', 'skipped': True}
    if config.get('storage_mode') == 'test':
        return {'message': '测试模式不保留字幕。', 'skipped': True}
    scheduler = ChannelScheduler(store, config, payload.get('_task_id'))
    scope = 'subtitles:youtube-origin'
    token = None
    if scheduler.enabled:
        def admit(db, stamp):
            state = scheduler._state(db, scope)
            delay = max(state.get('next_at', 0), state.get('cooldown_until', 0)) - stamp
            if delay > 0:
                return None, delay
            token, delay = scheduler._admit(db, stamp, [(scope, 1, 1)])
            if token:
                state = scheduler._state(db, scope)
                state['next_at'] = stamp + 10
                scheduler._save(db, scope, state)
            return token, delay
        token, delay = scheduler.transaction_call(admit)
        if not token:
            error = PipelineBusy('字幕请求正在退避，视频下载继续。')
            error.retry_after = max(2, delay)
            raise error
    try:
        budget = DiskBudget(store.directory, config)
        with budget.lease('cache', task=payload.get('_task_id'), reserve=16 * MIB) as lease:
            config['_temporary_directory'] = str(lease.path)
            options = build_ydl_options(config, payload['base'])
            options.update(skip_download=True, download_archive=None, writesubtitles=True, writeautomaticsub=True,
                           writeinfojson=False, writethumbnail=False, ignoreerrors=False,
                           outtmpl=str(video.with_suffix('')).replace('%', '%%') + '.%(ext)s',
                           retries=1, extractor_retries=1, socket_timeout=15, max_filesize=8 * MIB)
            def heartbeat(_):
                report({'stage': 'downloading_subtitles', 'speed': None, 'eta': None})
                lease.check()
            options['progress_hooks'] = [heartbeat]
            report({'stage': 'downloading_subtitles', 'speed': None, 'eta': None})
            if store and payload.get('_task_id') and not store.heartbeat(payload['_task_id'], store.owner):
                raise DownloadLeaseLost('字幕任务租约已失效。')
            with download_cookie_session(options) as session:
                with yt_dlp.YoutubeDL(session) as downloader:
                    failed = downloader.download(payload['urls'])
                    if failed:
                        raise RuntimeError('字幕补下载失败，视频已保留。')
            return {'message': '字幕补下载已完成；源视频没有所选语言时不生成字幕。', 'video_preserved': True}
    except Exception as error:
        status, delay = throttle_details(error)
        if scheduler.enabled and status in (429, 503):
            def cool(db, stamp):
                state = scheduler._state(db, scope)
                state.update(cooldown_until=stamp + (delay or 120), cooldown_status=status, cooldown_kind='subtitles')
                scheduler._save(db, scope, state)
            scheduler.transaction_call(cool)
        raise
    finally:
        if token:
            scheduler._release(token)
