"""Dispatch supported task actions without owning queue lifecycle."""
from pathlib import Path


def run_action(action, payload, report_progress=None, rotation_completed=None, store=None, worker_slot=0):
    from webapp.disk_budget import DiskPressure
    from Utils.runtime import configure_runtime
    configure_runtime()
    from webapp.media import VIDEO_SUFFIXES
    from webapp.environment import check_download_environment
    config, base = payload.get('config', {}), Path(payload['base'])
    if action == 'subtitles':
        from webapp.subtitles import run_subtitles
        return run_subtitles(store, payload, report_progress or (lambda value: None))
    if action == 'import':
        from webapp.bulk import run_import
        return run_import(store, payload, report_progress or (lambda value: None))
    if action == 'index':
        from webapp.catalog import MediaCatalog
        from Utils.config import video_directory
        return MediaCatalog(store).reindex(video_directory(config, base), report_progress)
    if action == 'check':
        if check_download_environment(config, base):
            raise RuntimeError('环境检查未通过，请根据日志补齐依赖。')
        return {'message': '环境检查通过'}
    if action == 'download':
        from webapp.worker.pipeline import run_download
        return run_download(payload, report_progress, rotation_completed, store, worker_slot)
    raise ValueError('当前 Web 服务只支持 YouTube 下载、导入、索引和环境检查。')
