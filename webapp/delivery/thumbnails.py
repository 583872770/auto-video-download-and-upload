"""Explicit quality policy and bounded local negative cache. No probe requests."""
import hashlib
import json
from pathlib import Path
import time

from webapp.disk_budget import file_lock


def candidates(info, strategy):
    def valid(url):
        return isinstance(url, str) and url.startswith(('http://', 'https://'))
    explicit = info.get('thumbnail')
    supplied = [item for item in info.get('thumbnails') or []
                if isinstance(item, dict) and valid(item.get('url'))]
    if strategy == 'source':
        # YouTube assigns preferences to both real and synthetic addresses;
        # only its explicit thumbnail is known to come from source metadata.
        youtube = info.get('extractor_key') == 'Youtube' or info.get('extractor') == 'youtube'
        urls = ([explicit] if valid(explicit) else []) + ([] if youtube else
            [item['url'] for item in reversed(supplied)])
    else:
        urls = [item['url'] for item in reversed(supplied)] + ([explicit] if valid(explicit) else [])
    return list(dict.fromkeys(urls))


class MissingThumbnails:
    TTL = 6 * 3600
    LIMIT = 2048

    def __init__(self, directory, task):
        self.path = Path(directory) / 'runtime/cache/thumbnail-misses.json'
        self.task = task

    def key(self, url):
        # Only digests reach disk, never signed URLs, credentials or query strings.
        return hashlib.sha256((self.task + '\n' + url).encode()).hexdigest()

    def read(self):
        try:
            data = json.loads(self.path.read_text(encoding='utf-8'))
            return {k: v for k, v in data.items() if isinstance(v, (int, float)) and v > time.time() - self.TTL}
        except (OSError, ValueError, AttributeError):
            return {}

    def contains(self, url):
        return self.key(url) in self.read()

    def record(self, url):
        try:
            self._record(url)
        except OSError:
            # An optional negative cache cannot turn an upstream 404 into a
            # local failure or hide the real authentication/rate-limit result.
            return

    def _record(self, url):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Cache contention must not stop a valid download. The cache is advisory.
        with file_lock(self.path.with_suffix('.lock')) as acquired:
            if not acquired:
                return
            data = self.read()
            data[self.key(url)] = time.time()
            data = dict(sorted(data.items(), key=lambda item: item[1])[-self.LIMIT:])
            temporary = self.path.with_suffix('.tmp')
            temporary.write_text(json.dumps(data), encoding='utf-8')
            temporary.replace(self.path)
