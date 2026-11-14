"""MediaLibraryService operations independent of route registration."""
from pathlib import Path
import re
import tempfile
import threading
from PIL import Image, UnidentifiedImageError
from Utils.config import resolve_path, video_directory
from Utils.paths import io_path
from Utils.runtime import ROOT
from webapp.media import VIDEO_SUFFIXES


class MediaLibraryService:
    def __init__(self, context):
        self.context = context

    def clear_media_library(self, data):
        from webapp.media_cleanup import clear_media, validate_clear_root
        if data.get('confirmation') != '清空':
            raise ValueError('请输入“清空”确认删除素材文件。')
        with self.context.media_lock, self.context.settings_lock:
            config, base = self.context.settings()
            protected = [ROOT, self.context.config_path, self.context.store.directory]
            if config.get('youtube_cookie'):
                protected.append(resolve_path(base, config['youtube_cookie']))
            root = validate_clear_root(video_directory(config, base), protected)
            if data.get('directory') != str(root):
                raise ValueError('素材目录已变化，请刷新页面后重新确认。')
            try:
                task = self.context.store.start_media_clear({'base': str(base), 'directory': str(root)})
            except ValueError as error:
                return dict(error=str(error)), 409

        def run_clear():
            stopped = threading.Event()
            def heartbeat():
                while not stopped.wait(10):
                    try:
                        if not self.context.store.heartbeat(task['id'], self.context.store.owner):
                            return
                    except Exception:
                        return
            keeper = threading.Thread(target=heartbeat, name='media-clear-heartbeat', daemon=True)
            keeper.start()
            try:
                def report(value):
                    if not self.context.store.update_progress(task['id'], value, self.context.store.owner):
                        raise RuntimeError('清空任务租约已失效，清理已停止。')
                validate_clear_root(root, protected)
                result = clear_media(self.context.catalog, root, report, config=config, base=base, protected=protected)
                self.context.store.finish(task['id'], result=result, worker_id=self.context.store.owner)
            except Exception as error:
                self.context.store.finish(task['id'], error=str(error), worker_id=self.context.store.owner)
            finally:
                stopped.set()
                keeper.join(timeout=2)
        threading.Thread(target=run_clear, name='media-clear', daemon=True).start()
        return dict(task), 202

    def import_media(self, video, image):
        if self.context.store.media_clear_busy():
            return dict(error='素材库正在清空，完成后再导入素材。'), 409
        if self.context.store.pending():
            return dict(error='请等待当前任务结束后再导入素材。'), 409
        if video is None or image is None or not video.filename or not image.filename:
            raise ValueError('请选择一个视频和对应的封面图片。')
        suffix = Path(video.filename).suffix.lower()
        cover_suffix = Path(image.filename).suffix.lower()
        if suffix not in VIDEO_SUFFIXES or cover_suffix not in ('.jpg', '.jpeg', '.png', '.webp'):
            raise ValueError('视频支持 MP4 / MKV / WebM / MOV；封面支持 JPG / PNG / WebP。')
        stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', Path(video.filename).stem).strip(' .')[:100]
        if not stem or stem.upper() in ('CON', 'PRN', 'AUX', 'NUL', *(f'COM{i}' for i in range(10)), *(f'LPT{i}' for i in range(10))):
            raise ValueError('视频文件名无效，请重命名后重试。')
        config, base = self.context.settings()
        root = Path(io_path(video_directory(config, base)))
        root.mkdir(parents=True, exist_ok=True)
        target, cover_target = root / (stem + suffix), root / (stem + cover_suffix)
        with self.context.media_lock:
            if self.context.store.media_clear_busy():
                return dict(error='素材库正在清空，完成后再导入素材。'), 409
            if self.context.store.pending():
                return dict(error='请等待当前任务结束后再导入素材。'), 409
            if target.exists() or any((root / (stem + ext)).exists() for ext in ('.jpg', '.jpeg', '.webp', '.png')):
                return dict(error='存在同名素材，请先重命名文件。'), 409
            with tempfile.TemporaryDirectory(prefix='.import-', dir=root) as staging:
                temporary_video = Path(staging) / ('video' + suffix)
                temporary_cover = Path(staging) / ('cover' + cover_suffix)
                video.save(temporary_video)
                image.save(temporary_cover)
                if not temporary_video.stat().st_size:
                    raise ValueError('视频文件为空。')
                try:
                    with Image.open(temporary_cover) as opened:
                        opened.verify()
                except (UnidentifiedImageError, OSError, ValueError) as error:
                    raise ValueError('封面不是有效图片。') from error
                temporary_cover.replace(cover_target)
                try:
                    temporary_video.replace(target)
                except OSError:
                    cover_target.unlink(missing_ok=True)
                    raise
        self.context.catalog.index_file(target, root)
        return dict(message='素材已导入，封面已自动匹配视频名称。', name=target.name), 201
