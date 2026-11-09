"""Compress bounded console responses; leave files, ranges and streams intact."""
import gzip

from flask import request


def register_compression(app):
    @app.after_request
    def compress_response(response):
        static = request.path.startswith('/static/')
        textual = response.mimetype in {
            'application/json', 'application/javascript', 'text/javascript',
            'text/css', 'text/html', 'image/svg+xml',
        }
        if not textual or request.method != 'GET':
            return response
        response.vary.add('Accept-Encoding')
        if (response.status_code != 200 or response.headers.get('Content-Encoding')
                or response.headers.get('Content-Disposition') or 'Range' in request.headers
                or response.cache_control.no_transform or request.accept_encodings['gzip'] <= 0):
            return response
        # send_file uses a file wrapper even for small static assets. Only those
        # bounded, known-size assets may be materialized; never buffer RPC/media.
        size = response.content_length
        if response.is_streamed and not static:
            return response
        if size is None or not 1024 <= size <= 2 * 1024 * 1024:
            return response
        response.direct_passthrough = False
        original = response.get_data()
        encoded = gzip.compress(original, compresslevel=4, mtime=0)
        if len(encoded) >= len(original):
            return response
        response.set_data(encoded)
        response.headers['Content-Encoding'] = 'gzip'
        etag, _ = response.get_etag()
        if etag:
            response.set_etag(etag, weak=True)
        return response
