"""Present yt-dlp output as videos rather than separate format tracks."""
import json
from pathlib import Path
import re

from Utils.paths import io_path

VIDEO_SUFFIXES = {'.mp4', '.mkv', '.webm', '.mov'}
IMAGE_SUFFIXES = ('.webp', '.jpg', '.jpeg', '.png')
FORMAT_TRACK = re.compile(r'^(?P<stem>.+)\.f[\w-]+$')


def media_items(directory):
    root = Path(io_path(directory))
    if not root.is_dir():
        return []
    files = {path.name: path for path in root.iterdir()
             if path.is_file() and path.resolve().parent == root.resolve()}
    images = {name.lower(): name for name in files if Path(name).suffix.lower() in IMAGE_SUFFIXES}
    groups = {}
    for path in files.values():
        if path.suffix.lower() not in VIDEO_SUFFIXES:
            continue
        match = FORMAT_TRACK.fullmatch(path.stem)
        stem = match['stem'] if match else path.stem
        # A metadata/cover companion or our [id] template identifies yt-dlp tracks.
        track = bool(match and (f'{stem}.info.json' in files or stem.endswith(']')
                               or any((stem + ext).lower() in images for ext in IMAGE_SUFFIXES)))
        groups.setdefault(stem if track else path.stem, []).append((path, track))
    result = []
    for stem, candidates in groups.items():
        # Prefer the finished container; don't show leftover audio/video formats twice.
        candidates.sort(key=lambda item: (item[1], item[0].suffix.lower() != '.mp4', item[0].name))
        path, track = candidates[0]
        info = {}
        if f'{stem}.info.json' in files:
            try:
                info = json.loads(files[f'{stem}.info.json'].read_text(encoding='utf-8'))
                if not isinstance(info, dict):
                    info = {}
            except (OSError, ValueError):
                pass
        cover = next((images[(stem + ext).lower()] for ext in IMAGE_SUFFIXES
                      if (stem + ext).lower() in images), None)
        stat = path.stat()
        result.append({'name': path.name, 'title': info.get('title') or stem,
                       'size': stat.st_size, 'modified': stat.st_mtime, 'cover': cover,
                       'playable': not track, 'status': 'incomplete' if track else 'ready',
                       'prepared': Path(io_path(root / f'{path.name}.parts')).is_dir()})
    return sorted(result, key=lambda item: (item['modified'], item['name']))
