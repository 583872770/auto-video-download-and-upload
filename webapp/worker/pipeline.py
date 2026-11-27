"""One authenticated yt-dlp source pipeline, followed by durable local delivery."""
from contextlib import contextmanager
from pathlib import Path
import time

from webapp.diagnostics import event, error_details
from webapp.download_policy import failure_evidence
from webapp.downloads import DownloadFailure, DownloadProgress
from webapp.worker.session import DownloadSession


class SourceExecution:
    def __init__(self, session):
        self.session = session

    @contextmanager
    def transmitting(self):
        session = self.session
        def waiting():
            session.progress.state.update(stage='waiting_transfer', speed=None, eta=None,
                transfer_admitted=False, transfer_wait_resource=session.scheduler.blocked_resource or 'resources')
            session.progress.save(force=True)
        with session.scheduler.permit('transfer', on_wait=waiting):
            session.progress.state.update(stage='waiting_connection', upstream_group='youtube-origin',
                transfer_admitted=True, transfer_wait_resource=None, speed=None, eta=None)
            session.progress.save(force=True)
            session.failure_phase = 'transfer'
            try:
                yield
            finally:
                session.progress.state['transfer_admitted'] = False


def run_download(payload, report_progress=None, rotation_completed=None, store=None, worker_slot=0):
    config, base = payload.get('config', {}), Path(payload['base'])
    if not store or not config.get('_cookie_pool_selected'):
        raise RuntimeError('下载任务必须通过账号池领取账号后执行。')
    if not config.get('_delivery_contract'):
        raise RuntimeError('下载任务缺少交付配置，请在任务中心配置下载内容。')
    from webapp.request_scheduler import RequestScheduler
    from webapp.download_metrics import meter_for
    from webapp.environment import require_native_environment
    from webapp.worker.connection import prepare_connection
    from webapp.youtube_auth import YouTubeAuth, YouTubeAuthRequired
    from webapp.cookie_profiles import CookieProfiles
    progress = DownloadProgress(report_progress or (lambda value: None))
    progress.state.update(worker_slot=worker_slot, download_method='yt_dlp', stage='checking_environment')
    progress.save(force=True)
    require_native_environment(config)
    config, health, snapshot = prepare_connection(config, payload, progress,
        worker_slot=worker_slot, rotation_completed=rotation_completed)
    try:
        CookieProfiles(base, store).restore_selected(config.get('youtube_cookie'))
    except ValueError as error:
        raise YouTubeAuthRequired({'state': 'invalid', 'message': str(error)}) from None
    progress.state.update(stage='checking_login');progress.save(force=True)
    YouTubeAuth(store).require(config, base, publish_latest=False,
        **({'proxy': snapshot['proxy'], 'node': snapshot['node']} if snapshot else {}))
    scheduler = RequestScheduler(store, config, payload.get('_task_id'), base=base)
    meter = meter_for(store.directory, worker_slot)
    session = DownloadSession(config=config, base=base, payload=payload, store=store,
        worker_slot=worker_slot, progress=progress, scheduler=scheduler, meter=meter,
        youtube=True, test_mode=config.get('storage_mode') == 'test',
        oss_mode=config.get('storage_mode') == 'oss', failure=DownloadFailure())
    event('execution_policy', policy_version=payload.get('policy_version'),
          storage_mode=config.get('storage_mode', 'local'),
          fragment_concurrency=config.get('fragment_concurrency', 4),
          download_attempts=config.get('download_attempts', 3), proxy_mode=config.get('proxy_mode'))
    started = time.monotonic()
    try:
        from webapp.delivery.executor import attempt, finish
        attempt(SourceExecution(session), payload['urls'])
        scheduler.outcome(success=True)
        meter.finish(True)
        if health:
            health.record(snapshot['node'], transferred_bytes=progress.state.get('downloaded_bytes', 0),
                          elapsed=session.transfer_seconds)
        event('provider_end', provider='yt_dlp', outcome='completed',
              network_seconds=progress.state.get('network_seconds', {}),
              elapsed_seconds=round(time.monotonic() - started, 3))
        return finish(session)
    except Exception as error:
        from webapp.disk_budget import DiskPressure, is_disk_write_pressure
        disk_monitor_error = payload.get('_disk_monitor_error')
        pressure = (disk_monitor_error and disk_monitor_error()) or error
        if isinstance(pressure, DiskPressure) or is_disk_write_pressure(pressure):
            # 空间不足属于本机资源等待，不计入来源失败，也不触发账号冷却。
            meter.finish(False)
            if pressure is not error:
                raise pressure from error
            raise
        evidence = failure_evidence(error, phase=session.failure_phase, source='youtube')
        progress.state['last_failure'] = evidence.public()
        progress.save(force=True)
        scheduler.outcome(success=False, error=error, phase=session.failure_phase)
        meter.finish(False, upstream_failure=evidence.http_status in (429, 503))
        if health and evidence.source not in ('local', 'oss') and evidence.kind not in ('account_rate_limit', 'request_rate_limit'):
            health.record(snapshot['node'], error=error)
        event('provider_end', provider='yt_dlp', outcome='failed',
              elapsed_seconds=round(time.monotonic() - started, 3), **error_details(error))
        raise
