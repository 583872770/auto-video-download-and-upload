"""Delete media and matching sidecars inside one explicitly confirmed local root."""
import os
from pathlib import Path
import time

from Utils.paths import io_path
from webapp.media import FORMAT_TRACK, IMAGE_SUFFIXES, VIDEO_SUFFIXES

AUDIO_SUFFIXES = {'.m4a', '.mp3', '.opus', '.ogg', '.wav', '.aac'}
SIDECAR_SUFFIXES = set(IMAGE_SUFFIXES) | AUDIO_SUFFIXES | {
    '.json', '.vtt', '.srt', '.ass', '.ttml', '.srv1', '.srv2', '.srv3', '.ytdl', '.part', '.temp'}


def validate_clear_root(root, protected=()):
    raw = Path(root).absolute()
    if raw.is_symlink() or raw.is_junction():
        raise ValueError('素材目录不能是符号链接或目录联接，请使用实际素材目录。')
    root = raw.resolve()
    if root == Path(root.anchor) or root == Path.home().resolve():
        raise ValueError('不能清空磁盘根目录或用户主目录。')
    for path in protected:
        if Path(path).resolve().is_relative_to(root):
            raise ValueError('素材目录包含项目、配置或状态目录，不能执行清空。请先设置独立素材目录。')
    return root


def clear_media(catalog, root, report=None):
    root = Path(root).resolve()
    deleted, total_bytes, failures = 0, 0, 0
    last_report = 0

    def progress(force=False):
        nonlocal last_report
        if report and (force or time.monotonic() - last_report >= 1.5):
            report({'stage': 'clearing_media', 'title': '正在清空素材库',
                    'deleted_files': deleted, 'deleted_bytes': total_bytes, 'failed_files': failures})
            last_report = time.monotonic()

    def walk(folder, remove_all=False):
        nonlocal deleted, total_bytes, failures
        previous_failures = failures
        if folder.is_symlink() or folder.is_junction() or not folder.resolve().is_relative_to(root):
            return
        try:
            with os.scandir(io_path(folder)) as entries:
                files, directories = [], []
                for entry in entries:
                    path = folder / entry.name
                    if entry.is_symlink() or path.is_junction():
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        if remove_all or not entry.name.startswith('.'):
                            directories.append(path)
                    elif entry.is_file(follow_symlinks=False):
                        files.append(path)
        except FileNotFoundError:
            return
        stems = set()
        for path in files:
            candidate = path.with_suffix('') if path.suffix.lower() == '.part' else path
            if candidate.suffix.lower() in VIDEO_SUFFIXES or path.name.endswith('.info.json'):
                stem = path.name[:-10] if path.name.endswith('.info.json') else candidate.stem
                match = FORMAT_TRACK.fullmatch(stem)
                stems.add(match['stem'] if match else stem)
        for directory in directories:
            if directory.name.endswith('.parts'):
                original = Path(directory.name[:-6])
                match = FORMAT_TRACK.fullmatch(original.stem)
                stem = match['stem'] if match else original.stem
                if remove_all or (original.suffix.lower() in VIDEO_SUFFIXES and stem in stems):
                    walk(directory, remove_all=True)
            else:
                walk(directory, remove_all)
        # Keep a video/metadata anchor when a sidecar is locked, so a retry can
        # still identify and remove the remaining matching files.
        def order(path):
            return 2 if path.name.endswith('.info.json') else int(path.suffix.lower() in VIDEO_SUFFIXES)
        for path in sorted(files, key=order):
            if not remove_all and order(path) and failures > previous_failures:
                continue
            related = remove_all
            if remove_all or path.suffix.lower() in VIDEO_SUFFIXES:
                related = True
            elif path.suffix.lower() in SIDECAR_SUFFIXES:
                # Test prefix boundaries, so "video" never removes "video-other".
                parts = path.name.split('.')
                related = any('.'.join(parts[:i]) in stems for i in range(1, len(parts)))
            if not related:
                continue
            progress()
            if path.is_symlink() or not path.resolve().is_relative_to(root):
                continue
            try:
                size = path.stat().st_size
                Path(io_path(path)).unlink()
                deleted += 1
                total_bytes += size
            except FileNotFoundError:
                continue
            except OSError:
                failures += 1
            progress()
        if folder != root:
            try:
                Path(io_path(folder)).rmdir()
            except OSError:
                pass

    progress(force=True)
    try:
        walk(root)
    finally:
        # Reconcile partial deletion too, retaining files that could not be removed.
        catalog.reindex(root)
    progress(force=True)
    result = {'message': f'素材库清理完成，删除 {deleted} 个文件，释放 {total_bytes} 字节。',
              'deleted_files': deleted, 'deleted_bytes': total_bytes, 'failed_files': failures}
    if failures:
        raise RuntimeError(f'部分素材未能删除（{failures} 个文件），请检查文件占用或权限后重试；素材索引已更新。')
    return result
