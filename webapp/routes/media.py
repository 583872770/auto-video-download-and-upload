"""Media HTTP endpoints."""
from pathlib import Path
from flask import jsonify, request, send_file
from Utils.config import video_directory
from Utils.paths import io_path
from webapp.media import VIDEO_SUFFIXES
from webapp.helpers import get_body, json_result
from webapp.delivery.listing import page as artifact_page


def register_media_routes(context, media_library):
    app = context.app
    catalog = context.catalog
    settings = context.settings
    store = context.store
    body = get_body

    @app.get('/api/media')
    def media():
        config, base = settings()
        root = Path(io_path(video_directory(config, base))).resolve()
        result = catalog.page(root, limit=int(request.args.get('limit', 24)),
                              cursor=request.args.get('cursor'), query=request.args.get('q', '')[:200])
        indexing = result.pop('indexing')
        return jsonify(**result, directory=str(root), indexing=indexing)

    @app.get('/api/media/assets')
    def media_assets():
        """Load local media and verified delivery outputs in one round trip."""
        config, base = settings()
        root = Path(io_path(video_directory(config, base))).resolve()
        media_page = catalog.page(root, limit=int(request.args.get('media_limit', 24)),
                                  cursor=request.args.get('media_cursor'), query=request.args.get('q', '')[:200])
        artifacts = artifact_page(store, instance_id=request.args.get('instance_id', ''),
                                   cursor=request.args.get('artifact_cursor'),
                                   query=request.args.get('q', ''),
                                   kind=request.args.get('kind', ''),
                                   language=request.args.get('language', ''),
                                   limit=int(request.args.get('artifact_limit', 50)))
        return jsonify(media=media_page, artifacts=artifacts, directory=str(root),
                       indexing=media_page['indexing'])

    @app.post('/api/media/reindex')
    def reindex_media():
        if store.media_clear_busy():
            return jsonify(error='素材库正在清空，完成后再同步本地文件。'), 409
        config, base = settings()
        task = store.create('index', {'config': config, 'base': str(base), 'scope': 'download'})
        return jsonify(task), 202

    @app.post('/api/media/clear')
    def clear_media_library():
        return json_result(media_library.clear_media_library(body()))

    @app.get('/media/videos/<path:name>')
    def video(name):
        # Block path traversal attempts
        if '..' in name.split('/') or name.startswith('/') or '\\' in name:
            return jsonify(error='视频不存在'), 404
        config, base = settings()
        root = Path(io_path(video_directory(config, base))).resolve()
        target = (root / name).resolve()
        if (not target.is_relative_to(root) or target.suffix.lower() not in VIDEO_SUFFIXES
                or not target.is_file()):
            return jsonify(error='视频不存在'), 404
        mimetype = {'.mp4': 'video/mp4', '.webm': 'video/webm',
                    '.mov': 'video/quicktime', '.mkv': 'video/x-matroska'}[target.suffix.lower()]
        # Werkzeug handles byte ranges so the native player can seek large files.
        return send_file(str(target), mimetype=mimetype, conditional=True)

    @app.get('/media/covers/<path:name>')
    def cover(name):
        # Block path traversal attempts
        if '..' in name.split('/') or name.startswith('/') or '\\' in name:
            return jsonify(error='封面不存在'), 404
        config, base = settings()
        root = Path(io_path(video_directory(config, base))).resolve()
        target = (root / name).resolve()
        if not target.is_relative_to(root) or target.suffix.lower() not in ('.jpg', '.jpeg', '.png', '.webp') or not target.is_file():
            return jsonify(error='封面不存在'), 404
        return send_file(target, conditional=True)

    @app.post('/api/media')
    def import_media():
        return json_result(media_library.import_media(request.files.get('video'), request.files.get('cover')))
