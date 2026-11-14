"""Verify completed files and record durable delivery receipts."""
import json
import os
import time
from pathlib import Path
from Utils.config import video_directory
from Utils.oss_storage import archive_key
from webapp.diagnostics import event
from webapp.local_processing import local_processing
from webapp.media import VIDEO_SUFFIXES


def record_completed_file(session, filename):
    config = session.config
    base = session.base
    payload = session.payload
    store = session.store
    progress = session.progress
    meter = session.meter
    catalog = session.catalog
    test_mode = session.test_mode
    oss_mode = session.oss_mode
    current_video = session.current_video
    output_files = session.output_files
    existing_files = session.existing_files
    deferred_files = session.deferred_files

    if store and payload.get('_business_job') and not store.heartbeat(payload['_task_id'], store.owner):
        from webapp.downloads import DownloadLeaseLost
        raise DownloadLeaseLost('任务清单已变更，当前执行已取消。')
    from Utils.paths import io_path
    path = Path(io_path(filename)).resolve()
    if path.suffix.lower() not in VIDEO_SUFFIXES:
        return
    if progress.state.get('download_method') in ('downmp3', 'yt1d', 'bergung'):
        from webapp.yt5s import verify_media
        try:
            with local_processing(payload.get('_disk_state') or (store.directory if store else base / '.cache'),
                    config, progress, 'verify_media', size=path.stat().st_size):
                verified = verify_media(path)
            streams = verified.get('streams', [])
            video = next((stream for stream in streams if stream.get('codec_type') == 'video'), {})
            event('media_verified', provider=progress.state.get('download_method'), video_id=current_video.get('id', ''),
                  width=video.get('width'), height=video.get('height'),
                  frame_rate=video.get('avg_frame_rate'),
                  video_codec=video.get('codec_name'),
                  duration_seconds=verified.get('format', {}).get('duration'))
        except RuntimeError:
            # A rejected HTTP error page may already have its final MP4
            # name. Do not let another provider mistake it for a video.
            if path not in existing_files and str(path) not in output_files:
                path.unlink(missing_ok=True)
            raise
    if catalog and not payload.get('_business_job') and not oss_mode:
        catalog.index_file(path, video_directory(config, base), downloaded_at=time.time())
    if (path in existing_files and not oss_mode) or str(path) in output_files:
        return
    size = path.stat().st_size
    output_files[str(path)] = size
    if test_mode and payload.get('_disk_workspace'):
        receipt = Path(payload['_disk_workspace']) / '.completed-files.json'
        temporary = receipt.with_suffix('.tmp')
        with temporary.open('w', encoding='utf-8') as stream:
            json.dump([{'path': name, 'size': amount, 'complete': True,
                        'method': progress.state.get('download_method'),
                        'video_id': current_video.get('id', ''),
                        'title': current_video.get('title', progress.state.get('title', ''))}
                       for name, amount in output_files.items()], stream)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(receipt)
    if oss_mode:
        deferred_files[str(path)] = dict(current_video)
    if store and payload.get('_task_id') and not oss_mode:
        if store.statistics.record_file(payload['_task_id'], path,
                current_video.get('id', ''), current_video.get('title', progress.state.get('title', '')),
                test_mode=test_mode):
            if catalog and payload.get('_business_job'):
                catalog.index_file(path, Path(config['_job_media_root']), downloaded_at=time.time())
            if not test_mode:
                session.billable_bytes += size
    meter.verified(size)


def finish_delivery(session, oss_storage, skipped_any, used_fallback):
    config = session.config
    base = session.base
    payload = session.payload
    store = session.store
    progress = session.progress
    oss_mode = session.oss_mode
    catalog = session.catalog
    output_files = session.output_files
    existing_files = session.existing_files
    deferred_files = session.deferred_files
    deferred_archives = session.deferred_archives
    oss_objects = []
    # Cloud delivery is outside provider/account health accounting. An OSS
    # error must not try a different YouTube provider or mark a Cookie bad.
    if oss_storage:
        from Utils.config import video_directory
        for filename in output_files:
            objects = oss_storage.upload_video(filename, video_directory(config, base),
                lambda value: (progress.state.update(value), progress.save(force=True)))
            oss_objects.extend(objects)
        # Commit delivery and deduplication only after ALL final files have
        # reached OSS. Failed attempts retain local files for the next try.
        for filename, info in deferred_files.items():
            path = Path(filename)
            if catalog:
                catalog.index_file(path, Path(config['_job_media_root']) if payload.get('_business_job')
                                   else video_directory(config, base), downloaded_at=time.time())
            if store and payload.get('_task_id') and store.statistics.record_file(payload['_task_id'], path,
                    info.get('id', ''), info.get('title', '')):
                session.billable_bytes += output_files[filename]
        if store:
            for identity, path in deferred_archives.items():
                store.archive_add(archive_key(config, identity), path,
                    **({'job_id': payload['_business_job']} if payload.get('_business_job') else {}))
    skipped = not output_files and ((skipped_any and not progress.streams) or bool(existing_files))
    return {'message': '已下载过，跳过重复下载。' if skipped else
            f'OSS 上传完成：{len(oss_objects)} 个文件已保存至 {config["oss_bucket"]}，本地副本已保留。' if oss_mode else
            '下载任务完成，请在素材库检查视频与封面。',
            'skipped': skipped, 'file_count': len(output_files), 'file_bytes': sum(output_files.values()),
            'billable_bytes': session.billable_bytes, 'download_method': progress.state['download_method'],
            'channel_id': progress.state.get('channel_id'),
            'storage_mode': config.get('storage_mode', 'local'),
            'oss_bucket': config.get('oss_bucket', '') if oss_mode else '',
            'oss_objects': oss_objects,
            'fallback_used': used_fallback}
