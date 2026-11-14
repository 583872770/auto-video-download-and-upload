"""Explicitly confirmed material reset; download deduplication is retained."""
from contextlib import ExitStack
import os
from pathlib import Path
import time

from sqlalchemy import select

from Utils.paths import io_path
from webapp.database import delivery_artifacts, tasks


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


def clear_media(catalog, root, report=None, *, config=None, base=None, protected=()):
    from Utils.runtime import ROOT
    from webapp.disk_budget import DiskBudget, file_lock
    from webapp.media_reset import reset_records

    store = catalog.store
    root = validate_clear_root(root, (ROOT, store.directory, *protected))
    local_id = store.fleet.id if store.fleet else 'local'
    preserved = {(Path(base) / 'videos.txt').absolute()} if base else set()
    cache_roots = [store.directory / part for part in (
        'runtime/downloads', 'runtime/cache', 'runtime/uploads', 'runtime/browser-profiles',
        'test-downloads', 'yt5s', 'imports', 'download-metrics')]
    for folder in cache_roots:
        if not folder.parent.resolve().is_relative_to(store.directory.resolve()):
            raise ValueError('下载缓存父目录不能指向实例状态目录之外。')
        if any(Path(path).resolve().is_relative_to(folder.absolute()) for path in protected):
            raise ValueError('下载缓存目录包含配置或凭据，请先调整保存位置。')
    with store.connect() as db:
        if db.execute(select(tasks.c.id).where(tasks.c.action.in_(('download', 'import', 'index')),
                tasks.c.status == 'running').limit(1)).first():
            raise RuntimeError('仍有下载、导入或索引任务运行，请停止后再清空。')
        artifacts = list(db.execute(select(delivery_artifacts.c.id, delivery_artifacts.c.path,
            delivery_artifacts.c.storage, delivery_artifacts.c.object_key, delivery_artifacts.c.available).where(
            delivery_artifacts.c.instance_id == local_id)).mappings())
    deleted = total_bytes = 0
    failures = []
    removed_cloud = set()
    last_report = 0

    def progress(force=False):
        nonlocal last_report
        if report and (force or time.monotonic() - last_report >= 1.5):
            report({'stage': 'clearing_media', 'title': '正在清空素材和关联数据',
                    'deleted_files': deleted, 'deleted_bytes': total_bytes, 'failed_files': len(failures)})
            last_report = time.monotonic()

    def remove(path, *, keep_root=False):
        nonlocal deleted, total_bytes
        if path.absolute() in preserved:
            return
        progress()
        try:
            # Remove the link itself; never enumerate its target, including junctions.
            if path.is_junction():
                path.rmdir()
                deleted += 1
            elif path.is_symlink():
                Path(io_path(path)).unlink()
                deleted += 1
            elif path.is_dir():
                with os.scandir(io_path(path)) as entries:
                    children = [path / entry.name for entry in entries]
                for child in children:
                    remove(child)
                if not keep_root:
                    Path(io_path(path)).rmdir()
            else:
                size = path.stat().st_size
                Path(io_path(path)).unlink()
                deleted += 1
                total_bytes += size
        except FileNotFoundError:
            pass
        except OSError:
            failures.append(path.name)

    def remove_log(path):
        before = len(failures)
        remove(path)
        if len(failures) > before:
            raise RuntimeError('部分任务日志未能删除，请检查文件占用或权限后重试。')

    progress(force=True)
    budget = DiskBudget(store.directory, config)
    with ExitStack() as locks:
        if not locks.enter_context(file_lock(store.directory / 'disk-cleanup.lock')):
            raise RuntimeError('磁盘清理正在执行，请稍后重试。')
        with budget.connect(write=False) as db:
            leases = db.execute('SELECT id FROM leases').fetchall()
        for lease in leases:
            if not locks.enter_context(file_lock(store.directory / 'disk-locks' / (lease['id'] + '.lock'))):
                raise RuntimeError('下载工作区仍被占用，请等待执行进程停止后重试。')
        try:
            remove(root, keep_root=True)
            for cache_root in cache_roots:
                remove(cache_root)
            # Remove an earlier registered output by its exact filename only.
            from webapp.media import VIDEO_SUFFIXES
            output_suffixes = VIDEO_SUFFIXES | {'.mp3', '.m4a', '.mka', '.opus', '.ogg', '.wav', '.aac',
                                               '.vtt', '.srt', '.ass', '.ttml', '.srv1', '.srv2', '.srv3', '.json3'}
            for artifact in artifacts:
                path = Path(artifact['path']).absolute()
                if not path.exists() and not path.is_symlink():
                    continue
                if (path.suffix.lower() not in output_suffixes or path.is_dir()
                        or any(path == Path(p).absolute() for p in protected)
                        or path.resolve() != path.absolute()):
                    failures.append('交付文件路径无效：' + path.name)
                    continue
                remove(path)
            cloud = [] if failures else [a for a in artifacts if a['storage'] == 'oss' and a['object_key']]
            if any(a['storage'] == 'oss' and a['available'] and not a['object_key'] for a in artifacts):
                failures.append('云端交付记录缺少对象地址，无法确认已清空')
            if cloud:
                from Utils.oss_storage import OSSStorage
                storage = OSSStorage(config or store.runtime())
                for key in dict.fromkeys(a['object_key'] for a in cloud):
                    progress()
                    with store.connect() as db:
                        shared = db.execute(select(delivery_artifacts.c.id).where(
                            delivery_artifacts.c.instance_id != local_id,
                            delivery_artifacts.c.storage == 'oss', delivery_artifacts.c.object_key == key).limit(1)).first()
                    if shared:
                        continue
                    try:
                        storage.bucket.delete_object(key)
                        if storage.bucket.object_exists(key):
                            failures.append('云端交付文件仍存在')
                        else:
                            removed_cloud.add(key)
                    except Exception:
                        failures.append('云端交付文件删除失败，请检查 OSS 权限或网络')
            for folder in (root, *cache_roots):
                if folder.is_dir():
                    with os.scandir(io_path(folder)) as entries:
                        if any((folder / entry.name).absolute() not in preserved for entry in entries):
                            failures.append(folder.name)
            if failures:
                raise RuntimeError(f'部分素材未能删除（{len(failures)} 个文件或目录），请检查占用、权限和清理日志后重试。')
            progress(force=True)
            records = reset_records(store, local_id, remove_log)
            with budget.connect() as db:
                db.execute('DELETE FROM leases')
                db.execute('DELETE FROM requirements')
        finally:
            catalog.reindex(root)
            with store.connect() as db:
                for artifact in artifacts:
                    if ((artifact['storage'] == 'local' and not Path(artifact['path']).is_file())
                            or (artifact['storage'] == 'oss' and artifact['object_key'] in removed_cloud)):
                        db.execute(delivery_artifacts.update().where(
                            delivery_artifacts.c.id == artifact['id']).values(available=0))
    progress(force=True)
    return {'message': f'素材和关联数据已清空，删除 {deleted} 个文件，仅保留下载去重记录。',
            'deleted_files': deleted, 'deleted_bytes': total_bytes, 'failed_files': 0, **records}
