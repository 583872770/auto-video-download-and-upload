"""Coalesce opt-in UI polls without caching execution or explicit refreshes."""
from flask import request

from webapp.read_cache import ReadCache


class DisplayCache:
    def __init__(self, app):
        self.cache = ReadCache(ttl=1, maximum=64)

        @app.after_request
        def invalidate_after_write(response):
            if request.method not in ('GET', 'HEAD', 'OPTIONS'):
                self.cache.clear()
            return response

    def read(self, key, load):
        if request.method == 'GET' and request.args.get('poll') == '1':
            return self.cache.read(key, load)
        # Manual refreshes must also evict the preceding display snapshot.
        self.cache.clear()
        return load()


def display_cache(app):
    if 'display_cache' not in app.extensions:
        app.extensions['display_cache'] = DisplayCache(app)
    return app.extensions['display_cache']
