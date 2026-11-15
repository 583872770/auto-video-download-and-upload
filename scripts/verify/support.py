"""Shared helpers for isolated browser checks."""
from werkzeug.serving import WSGIRequestHandler


class QuietHandler(WSGIRequestHandler):
    def log_request(self, *args, **kwargs):
        pass
