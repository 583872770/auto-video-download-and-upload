"""Bounded local counters; never persist resolved URLs or request credentials."""
import json
import os
from pathlib import Path
import time

from webapp.throughput import _atomic


class DownloadMeter:
    def __init__(self, directory, slot=0):
        self.path = Path(directory) / 'download-metrics' / f'{slot}.json'
        self.data = {'pid': os.getpid(), 'generation': f'{os.getpid()}-{time.time_ns()}',
                     'received_bytes': 0, 'verified_bytes': 0, 'succeeded': 0, 'failed': 0,
                     'channels': {}, 'groups': {}}
        self.last = 0
        self.streams = {}
        self.channel = self.group = None
        self.resolve_seconds = 0

    def begin(self, channel):
        self.channel, self.group = channel.id, channel.upstream_group
        self.streams = {}
        self.resolve_seconds = 0

    def _add(self, key, value):
        self.data[key] = self.data.get(key, 0) + value
        for category, identity in (('channels', self.channel), ('groups', self.group)):
            if identity:
                counters = self.data[category].setdefault(identity, {})
                counters[key] = counters.get(key, 0) + value

    def hook(self, event):
        info = event.get('info_dict') or {}
        if info.get('ext') in ('vtt', 'srt', 'ass', 'ttml', 'json3'):
            return
        key = event.get('filename') or info.get('format_id') or 'video'
        size = max(0, event.get('downloaded_bytes') or 0)
        previous = self.streams.get(key, 0)
        # A restart within one attempt can reset the progress counter.
        self._add('received_bytes', max(0, size - previous))
        self.streams[key] = size
        self.publish()

    def verified(self, size):
        self._add('verified_bytes', size)
        self.publish(force=True)

    def resolved(self, seconds):
        self.resolve_seconds += seconds
        self._add('resolved', 1)
        self._add('resolve_seconds', seconds)
        self.publish(force=True)

    def finish(self, success, *, upstream_failure=False):
        if not success and upstream_failure:
            self._add('upstream_failed', 1)
        self._add('succeeded' if success else 'failed', 1)
        self.publish(force=True)

    def publish(self, force=False):
        if force or time.monotonic() - self.last >= 5:
            self.data['time'] = time.time()
            _atomic(self.path, self.data)
            self.last = time.monotonic()


def read_counters(directory):
    results = {}
    for path in (Path(directory) / 'download-metrics').glob('*.json'):
        try:
            if path.stat().st_size > 128 * 1024:
                continue
            value = json.loads(path.read_text(encoding='utf-8'))
            results[value['generation']] = value
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return results


_meters = {}


def meter_for(directory, slot):
    key = (str(Path(directory).resolve()), slot)
    if key not in _meters:
        _meters[key] = DownloadMeter(directory, slot)
    return _meters[key]
