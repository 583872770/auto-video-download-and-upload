"""Utility functions for task store."""
import hashlib
from datetime import datetime
from urllib.parse import parse_qs, urlsplit


def now():
    """Current timestamp in ISO format."""
    return datetime.now().astimezone().isoformat(timespec='seconds')


def source_identity(url):
    """Extract video ID and source key from URL.

    Returns:
        (video_id, source_key):
            - video_id: YouTube video ID or None
            - source_key: 'youtube:<id>' or 'url:<hash>'
    """
    parsed = urlsplit(url or '')
    host = (parsed.hostname or '').lower().removeprefix('www.')
    video_id = None

    if host in ('youtube.com', 'm.youtube.com', 'music.youtube.com'):
        video_id = parse_qs(parsed.query).get('v', [None])[0]
        if not video_id and parsed.path.startswith(('/shorts/', '/live/')):
            video_id = parsed.path.split('/')[2] if len(parsed.path.split('/')) > 2 else None
    elif host == 'youtu.be':
        video_id = parsed.path.strip('/')

    if video_id:
        return video_id, 'youtube:' + video_id
    else:
        url_hash = hashlib.sha256((url or '').encode()).hexdigest()
        return None, 'url:' + url_hash
