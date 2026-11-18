"""Durable, instance-local delivery work. Never acquires source/account permits."""
import json
from pathlib import Path
import shutil
import time
import uuid

from sqlalchemy import select
from webapp.database import delivery_work, tasks
from webapp.delivery import artifacts as ledger
from webapp.delivery.verify import media_output, convert, verify, inspect, LocalDeliveryError
from webapp.disk_budget import DiskBudget, DISK_FIELDS, safe_path, size_of, file_lock, alive
from webapp.downloads import DownloadProgress


def enqueue(session, *, resume_network=False):
    # Persist only settings needed for local delivery, never Cookies or resolved
    # source URLs. The parent task retains its pinned contract/session snapshot.
    keys = ('path', 'dir', 'storage_mode', 'shard_downloads', '_delivery_contract',
            '_delivery_version', '_oss_namespace', '_task_storage')
    config = {key: session.config[key] for key in keys if key in session.config}
    config['_delivery_contract'] = {key: value for key, value in config['_delivery_contract'].items() if key != 'storage'}
    plan = dict(jobs=session.local_jobs, config=config, base=str(session.base),
        current_video=session.current_video, output_files=session.output_files,
        billable_bytes=session.billable_bytes, progress=session.progress.state, resume_network=resume_network)
    with session.store.connect() as db:
        task = ledger.fenced_run(session.store, db, session.payload['_task_id'])
        db.execute(session.store.db.upsert(delivery_work, dict(task_id=session.payload['_task_id'],
            instance_id=session.store.fleet.id if session.store.fleet else 'local',
            workspace=session.payload['_disk_workspace'], disk_lease=session.payload['_disk_lease'],
            plan=json.dumps(plan, ensure_ascii=False), created_at=time.time(), attempts=0,
            source_attempts=task['attempts']), ['task_id']))
    if not resume_network:
        session.progress.state.update(stage='waiting_processing', speed=None, eta=None,
            cookie_profile_id=None, cookie_profile_name=None, wait_reason='网络下载完成，等待本地处理。')
        session.progress.save(force=True)
    # On source failure, keep the actual stage/account until the queue records
    # its evidence and recovery decision. Settlement then releases the account.
    return dict(processing_pending=True, message='网络下载完成，已交给本地处理队列。',
                file_count=len(session.output_files), file_bytes=sum(session.output_files.values()))


def checkpoint(store, task_id, plan):
    with store.connect() as db:
        ledger.fenced_run(store, db, task_id)
        db.execute(delivery_work.update().where(delivery_work.c.task_id == task_id).values(
            plan=json.dumps(plan, ensure_ascii=False)))


def run(store, task_id, work, report, *, slot=0):
    from webapp.delivery.executor import ready, publish, finish
    from webapp.worker.session import DownloadSession
    from webapp.download_metrics import meter_for
    from Utils.config import video_directory
    from webapp.local_processing import local_processing
    from webapp.delivery.contracts import LABELS
    plan = json.loads(work['plan'])
    config = dict(plan['config'])
    runtime = store.runtime()
    config.update({key: runtime[key] for key in DISK_FIELDS if key in runtime})
    # OSS credentials are resolved from the pinned task's storage profile.
    payload = store.get(task_id, private=True)['payload']
    if config.get('storage_mode') == 'oss':
        config.update({key: value for key, value in payload['config'].items() if key.startswith('oss')})
    budget = DiskBudget(store.directory, config)
    lease = budget.adopt(work['disk_lease'], task_id, work['workspace'])
    lease.preserve = True  # Settlement commits before any workspace is deleted.
    if config.get('storage_mode') != 'test':
        lease.media_directory = video_directory(config, plan['base'])
    progress = DownloadProgress(lambda value: (lease.check(), report(value)))
    # Carry acquisition timings across process boundaries; initializing a new
    # writer must not erase extraction/download durations or reset elapsed time.
    progress.started_at -= max(0, float(plan['progress'].get('elapsed') or 0))
    progress.stage_seconds = dict(plan['progress'].get('stage_seconds') or {})
    progress.current_stage = (plan['progress'].get('download_method'), 'processing')
    progress.stage_started_at = time.time()
    progress.state.update(plan['progress'], stage='processing', speed=None, eta=None,
        cookie_profile_id=None, cookie_profile_name=None, wait_reason=None, retry_at=None, worker_slot=slot)
    session = DownloadSession(config=config, base=Path(plan['base']),
        payload={'_task_id': task_id, '_disk_workspace': work['workspace']}, store=store,
        worker_slot=slot, progress=progress, scheduler=None, meter=meter_for(store.directory, slot),
        youtube=False, test_mode=config.get('storage_mode') == 'test',
        oss_mode=config.get('storage_mode') == 'oss',
        current_video=plan['current_video'], output_files=dict(plan['output_files']),
        billable_bytes=plan['billable_bytes'])
    with lease, local_processing(store.directory, runtime, progress, 'delivery'):
        from webapp.control import terminate_own_descendants
        lease.start_monitor(terminate_own_descendants)
        source_probes = {}
        # A progressive file already contains both tracks. Extract any requested
        # separate tracks before moving that shared source as the AV output.
        ordered = sorted(plan['jobs'], key=lambda j: j['kind'] == 'av' and len(set(j['inputs'])) == 1)
        for job in ordered:
            kind, language = job['kind'], job.get('language', '')
            progress.state.update(stage='processing', output_kind=kind, output_language=language)
            progress.save(force=True)
            if ready(session, kind, language):
                continue
            inputs = [safe_path(path, lease.path) for path in job['inputs']]
            destination = Path(job['destination']) if job.get('destination') else None
            if destination and destination.is_file():
                # A crash can happen after the atomic move, before the receipt.
                publish(session, destination, kind, language, retained=True,
                        subtitle_source=job.get('subtitle_source', ''))
                continue
            if not all(path.is_file() for path in inputs):
                raise LocalDeliveryError('本地处理所需源文件缺失，请清理该来源的待处理内容后重新下载。')
            if kind == 'subtitle':
                extension = '.' + job['format']
                direct = inputs[0].suffix == extension
                media, mp4 = None, False
                if direct:
                    verify(inputs[0], kind)
            else:
                mp4 = job['mp4']
                for path in inputs:
                    if path not in source_probes:
                        source_probes[path] = inspect(path)
                probes = [source_probes[path] for path in inputs]
                if job.get('auto'):
                    video = next((s.get('codec_name') for s in probes[0][1]['streams'] if s.get('codec_type') == 'video'), '')
                    audio = next((s.get('codec_name') for s in probes[-1][1]['streams'] if s.get('codec_type') == 'audio'), '')
                    mp4 = video in ('h264', 'hevc', 'av1', 'mpeg4') and audio in ('aac', 'alac', 'mp3')
                media = media_output(inputs, kind, mp4=mp4, mp3=job['mp3'], probes=probes)
                extension, direct = media['extension'], media['direct']
            output = inputs[0] if direct else inputs[0].parent / ('processed-' + kind + extension)
            if not direct:
                # Reserve the temporary conversion peak before invoking FFmpeg.
                lease.check(size_of(lease.path) + sum(p.stat().st_size for p in set(inputs)))
                convert(inputs, output, kind, mp4=mp4, mp3=job.get('mp3', False), media=media)
            print(f'{LABELS[kind]}：' + ('原始文件符合要求，直接交付。' if direct else '格式处理完成，正在交付。'), flush=True)
            if not destination:
                root = video_directory(config, plan['base'])
                if config.get('shard_downloads'):
                    root /= store.get(task_id)['video_id']
                destination = root / (uuid.uuid4().hex + extension)
                destination.parent.mkdir(parents=True, exist_ok=True)
                job['destination'] = str(destination)
                checkpoint(store, task_id, plan)
            progress.state.update(local_operation='原始文件直接交付' if direct else '格式处理完成')
            publish(session, output, kind, language, verified=True, destination=destination,
                    subtitle_source=job.get('subtitle_source', ''))
            plan.update(output_files=session.output_files, billable_bytes=session.billable_bytes,
                        progress=dict(progress.state))
            checkpoint(store, task_id, plan)
        result = finish(session)
        if result.get('file_count'):
            budget.record_video_size(max(result['file_bytes'], lease.estimate), merged=False)
        if plan.get('source_failure'):
            result['source_failed'] = True
            result['message'] = '已下载内容已交付；部分下载失败：' + plan['source_failure']['error']
        elif plan['resume_network']:
            result['network_pending'] = True
        result.update(test_mode=session.test_mode, _processing_workspace=work)
        # No new network traffic. Keep the acquisition byte counter for logs.
        if session.test_mode and not plan.get('source_failure'):
            result['message'] = '测试下载完成，处理结果已登记。'
        return result


def retains_test_workspace(store, lease):
    """Retire terminal test plans only after every planned output is durable.

    The caller owns the disk lease lock and has verified the writers stopped.
    Unfinished conversions, uploads and queued retries must keep their inputs.
    """
    with store.connect() as db:
        task = db.execute(select(tasks).where(tasks.c.id == lease['task']).with_for_update()).mappings().first()
        work = db.execute(select(delivery_work).where(delivery_work.c.task_id == lease['task'],
            delivery_work.c.disk_lease == lease['id']).with_for_update()).mappings().first()
        if not work:
            return False
        if (lease['category'] != 'test' or not task or task['status'] not in ('succeeded', 'failed')
                or work['workspace'] != lease['path']
                or work['instance_id'] != (store.fleet.id if store.fleet else 'local')):
            return True
        plan = json.loads(work['plan'])
        config = plan['config']
        if config.get('storage_mode') != 'test':
            return True
        for job in plan['jobs']:
            artifact = dict(kind=job['kind'], language=job.get('language', ''),
                variant=ledger.variant(config['_delivery_contract'], job['kind']))
            artifact['id'] = ledger.artifact_identity(task['batch_id'], task['video_id'],
                config['_delivery_contract'], artifact['kind'], artifact['language'], True)
            if not ledger.recorded_test_output(db, task, artifact):
                return True
        db.execute(delivery_work.delete().where(delivery_work.c.task_id == task['id'],
            delivery_work.c.disk_lease == lease['id']))
        return False


def cleanup_completed(store, work):
    """Targeted best-effort cleanup after fenced task settlement; sweep retries."""
    with store.connect() as db:
        if db.execute(select(delivery_work.c.task_id).where(delivery_work.c.task_id == work['task_id'])).first():
            return False
        status = db.execute(select(tasks.c.status).where(tasks.c.id == work['task_id'])).scalar_one_or_none()
        if status not in (None, 'queued', 'succeeded', 'failed', 'removed', 'cancelled'):
            return False
    budget = DiskBudget(store.directory, store.runtime())
    with file_lock(store.directory / 'disk-locks' / (work['disk_lease'] + '.lock')) as owned:
        if not owned:
            return False
        with budget.connect(write=False) as db:
            row = db.execute('SELECT * FROM leases WHERE id=?', (work['disk_lease'],)).fetchone()
        if not row or alive(json.loads(row['owner'])) or any(alive(c) for c in json.loads(row['children'])):
            return False
        path = safe_path(row['path'], store.directory)
        if path.exists():
            shutil.rmtree(path)
        with budget.connect() as db:
            db.execute('DELETE FROM leases WHERE id=?', (work['disk_lease'],))
        if row['category'] == 'test':
            try:
                path.parent.rmdir()
            except OSError:
                pass
        return True
