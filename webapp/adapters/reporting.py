"""yt-dlp reporting adapter for progress, archives and file verification."""
from pathlib import Path
import re
import time
from Utils.oss_storage import archive_key
from webapp.diagnostics import event
from webapp.local_processing import local_processing
from webapp.parallel_http import ParallelYoutubeDL
from webapp.youtube_auth import YouTubeAuth, YouTubeAuthRequired


def build_reporting_downloader(session):
    config = session.config
    base = session.base
    payload = session.payload
    store = session.store
    progress = session.progress
    test_mode = session.test_mode
    oss_mode = session.oss_mode
    current_video = session.current_video
    output_files = session.output_files
    existing_files = session.existing_files
    deferred_archives = session.deferred_archives
    class ReportingDownloader(ParallelYoutubeDL):
        skipped = False

        def run_pp(self, pp, infodict):
            with local_processing(payload.get('_disk_state') or (store.directory if store else base / '.cache'),
                    config, progress, type(pp).__name__):
                return super().run_pp(pp, infodict)

        def to_screen(self, message, *args, **kwargs):
            if str(message).startswith('[download] Sleeping '):
                sleeping = re.match(r'\[download\] Sleeping (\d+(?:\.\d+)?) seconds\b', str(message))
                progress.state.update(stage='waiting_request', speed=None, eta=None,
                                      request_wait_reason='file_start',
                                      request_wait_until=time.time() + float(sleeping[1]) if sleeping else None)
                progress.save(force=True)
            return super().to_screen(message, *args, **kwargs)

        def report_warning(self, message, *args, **kwargs):
            session.failure.warning(message)
            progress.state['warning_counts'] = dict(session.failure.warnings)
            if (config.get('_cookie_pool_selected') or config.get('youtube_pacing_mode') == 'adaptive') and session.failure.cookie_rejected:
                # Stop at the first invalid-session warning, before yt-dlp
                # falls back to more clients or downloads anonymously.
                result = YouTubeAuth(store).reject(session.failure.message(),
                    node=config.get('_resolved_clash_node', ''), profile_id=config.get('_cookie_pool_selected'))
                raise YouTubeAuthRequired(result)
            return super().report_warning(message, *args, **kwargs)

        def report_error(self, message, *args, **kwargs):
            session.failure.error(message)
            return super().report_error(message, *args, **kwargs)

        def process_info(self, info_dict):
            # yt-dlp removes requested_formats before individual stream hooks.
            from Utils.paths import io_path
            current_video.update(id=info_dict.get('id', ''), title=info_dict.get('title', ''))
            event('format_selected', provider=progress.state.get('download_method'),
                  video_id=info_dict.get('id'), format_id=info_dict.get('format_id'),
                  width=info_dict.get('width'), height=info_dict.get('height'), fps=info_dict.get('fps'),
                  video_codec=info_dict.get('vcodec'), audio_codec=info_dict.get('acodec'),
                  quality_source='requested' if progress.state.get('download_method') == 'downmp3' else 'provider_reported')
            for filename in (self.prepare_filename(info_dict), info_dict.get('filepath')):
                if filename:
                    path = Path(io_path(filename)).resolve()
                    if path.is_file() and str(path) not in output_files:
                        existing_files.add(path)
            progress.begin_video(info_dict)
            return super().process_info(info_dict)

        def in_download_archive(self, info_dict):
            if test_mode:
                return False
            archived = store.archive_has(archive_key(config, self._make_archive_id(info_dict)), **({'job_id': payload['_business_job']} if payload.get('_business_job') else {})) if store else (False if oss_mode else super().in_download_archive(info_dict))
            if archived:
                self.skipped = True
                progress.state.update(stage='skipped', title=str(info_dict.get('title') or info_dict.get('id') or ''),
                                      percent=100, speed=None, eta=None)
                progress.save(force=True)
            return archived

        def record_download_archive(self, info_dict):
            if test_mode:
                return
            if oss_mode:
                identity = self._make_archive_id(info_dict)
                if identity:
                    deferred_archives[identity] = info_dict.get('filepath') or info_dict.get('_filename')
                return
            if store:
                identity = self._make_archive_id(info_dict)
                if identity:
                    store.archive_add(identity, info_dict.get('filepath') or info_dict.get('_filename'), **({'job_id': payload['_business_job']} if payload.get('_business_job') else {}))
            else:
                super().record_download_archive(info_dict)

    return ReportingDownloader
