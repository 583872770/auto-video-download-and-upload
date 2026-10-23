"""Flask web console. Run `python app.py`, then open http://127.0.0.1:5000."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import sys
import tempfile
import threading
import uuid

from flask import Flask, jsonify, render_template, request, send_file, session
from PIL import Image, UnidentifiedImageError
from werkzeug.exceptions import HTTPException

from Utils.config import DEFAULT_CONFIG, load_config, resolve_path, video_directory
from Utils.paths import io_path
from Utils.runtime import ROOT, configure_runtime
from Utils.youtube_login import parse_youtube_cookie_file, save_youtube_cookies
from main import VIDEO_SUFFIXES
from webapp.settings import DEFAULTS, DOWNLOAD_FIELDS, save_settings, urls_from, validate_settings
from webapp.catalog import MediaCatalog
from webapp.tasks import TaskRunner, TaskStore
from webapp.clash import ClashError, ClashManager
from webapp.clash_routes import register_clash

WEB_PLATFORMS = ('douyin', 'kuaishou')
ACTIONS = ('download', 'preview', 'prepare', 'publish', 'login', 'check')


def create_app(config_path=None, state_dir=None, *, start_worker=True):
    configure_runtime()
    app = Flask(__name__)
    app.json.ensure_ascii = False
    config_path = Path(config_path or DEFAULT_CONFIG).resolve()
    store = TaskStore(state_dir or ROOT / '.cache' / 'web')
    secret_file = store.directory / 'session.key'
    try:
        with secret_file.open('xb') as output:
            output.write(secrets.token_bytes(32))
    except FileExistsError:
        pass
    app.config.update(
        SECRET_KEY=secret_file.read_bytes(), MAX_CONTENT_LENGTH=8 * 1024 ** 3,
        SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Strict',
        TRUSTED_HOSTS=os.environ.get('VIDEO_WEB_HOSTS', f'localhost,127.0.0.1,[::1],{socket.gethostname()}').split(','),
    )
    app.extensions['task_store'] = store
    catalog = MediaCatalog(store)
    app.extensions['media_catalog'] = catalog
    settings_lock = threading.RLock()
    media_lock = threading.Lock()
    clash = ClashManager(store.directory / 'clash')
    app.extensions['clash'] = clash

    def settings():
        with settings_lock:
            return load_config(config_path)

    store.set_runtime({**DEFAULTS, **settings()[0]})

    def body():
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            raise ValueError('请提交 JSON 对象。')
        return data

    @app.before_request
    def csrf_protection():
        if 'csrf' not in session:
            session['csrf'] = secrets.token_urlsafe(32)
        if request.method not in ('GET', 'HEAD', 'OPTIONS'):
            token = request.headers.get('X-CSRF-Token', '')
            if not secrets.compare_digest(token, session['csrf']):
                return jsonify(error='页面会话已失效，请刷新后重试。'), 403

    @app.after_request
    def response_headers(response):
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Referrer-Policy'] = 'same-origin'
        response.headers['Content-Security-Policy'] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        )
        if request.path.startswith('/api/'):
            response.headers['Cache-Control'] = 'no-store'
        return response

    @app.errorhandler(ValueError)
    def invalid(error):
        return jsonify(error=str(error)), 400

    @app.errorhandler(OSError)
    def filesystem_error(error):
        app.logger.exception('文件操作失败')
        return jsonify(error=f'文件操作失败：{error}'), 400

    @app.errorhandler(HTTPException)
    def http_error(error):
        return jsonify(error=error.description), error.code

    from sqlalchemy.exc import SQLAlchemyError

    @app.errorhandler(SQLAlchemyError)
    def database_error(error):
        app.logger.exception('数据库操作失败')
        return jsonify(error='数据库暂时不可用，请检查数据库服务。'), 503

    @app.get('/')
    def index():
        return render_template('index.html', csrf=session['csrf'])

    @app.get('/api/config')
    def read_config():
        config, base = settings()
        editable = {key: config.get(key, default) for key, default in DEFAULTS.items()}
        return jsonify(config=editable, config_path=str(config_path), media_directory=str(video_directory(config, base)))

    @app.put('/api/config')
    def update_config():
        data = body()
        if any(key not in DEFAULTS for key in data):
            raise ValueError('包含不支持在页面修改的配置项。')
        with media_lock, settings_lock:
            config, _ = settings()
            if store.pending() and any(key in data and data[key] != config.get(key, DEFAULTS[key])
                                       for key in ('proxy', 'proxy_mode')):
                return jsonify(error='请等待下载队列结束后再修改代理连接方式。'), 409
            config.update(data)
            validate_settings(config, download_only=set(data) <= DOWNLOAD_FIELDS)
            save_settings(config_path, config)
            store.set_runtime({**DEFAULTS, **config})
        return jsonify(message='配置已保存，新任务将使用这份配置。')

    @app.get('/api/status')
    def status():
        config, base = settings()
        accounts = {}
        for platform in WEB_PLATFORMS:
            path = resolve_path(base, config.get(f'{platform}_cookie', f'Cookies/{platform}.json'))
            valid = False
            if path.is_file():
                try:
                    state = json.loads(path.read_text(encoding='utf-8'))
                    valid = isinstance(state, dict) and isinstance(state.get('cookies'), list) and bool(state['cookies']) and isinstance(state.get('origins'), list)
                except (ValueError, OSError):
                    pass
            accounts[platform] = {'saved': valid, 'visibility': config.get(f'{platform}_visibility', 'public')}
        tools = {name: shutil.which(name) for name in ('ffmpeg', 'ffprobe', 'node', 'deno')}
        tools['playwright'] = importlib.util.find_spec('playwright') is not None
        youtube_file = resolve_path(base, config['youtube_cookie']) if config.get('youtube_cookie') else None
        youtube = {'saved': bool(youtube_file and youtube_file.is_file() and youtube_file.stat().st_size),
                   'path': str(youtube_file) if youtube_file else None}
        return jsonify(tools=tools, accounts=accounts, python=sys.version.split()[0], database=store.db.engine.dialect.name,
                       workers=store.workers(), worker_count=config.get('worker_count', 1),
                       youtube=youtube, media_directory=str(video_directory(config, base)), timezone='服务器本地时间')

    @app.post('/api/youtube/cookie')
    def upload_youtube_cookie():
        request.max_content_length = 2 * 1024 * 1024
        uploaded = request.files.get('cookie')
        if uploaded is None or not uploaded.filename or Path(uploaded.filename).suffix.lower() != '.txt':
            raise ValueError('请选择 Netscape 格式的 Cookie .txt 文件。')
        cookies = parse_youtube_cookie_file(uploaded.stream.read(1024 * 1024 + 1))
        with media_lock, settings_lock:
            if store.pending():
                return jsonify(error='请等待当前任务结束后再更新 Cookie。'), 409
            config, base = settings()
            # Use a fixed destination, never a client-supplied filename or path.
            target = base / 'Cookies' / 'youtube.txt'
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists() and not target.is_file():
                raise ValueError('Cookie 保存位置不是文件，请检查 Cookies/youtube.txt。')
            with tempfile.TemporaryDirectory(prefix='.youtube-import-', dir=target.parent) as staging:
                backup = Path(staging) / 'previous.txt'
                if target.is_file():
                    shutil.copy2(target, backup)
                save_youtube_cookies(cookies, target)
                try:
                    config['youtube_cookie'] = 'Cookies/youtube.txt'
                    save_settings(config_path, config)
                except OSError:
                    if backup.is_file():
                        backup.replace(target)
                    else:
                        target.unlink(missing_ok=True)
                    raise
        return jsonify(message='YouTube Cookie 已保存，后续下载将自动使用。', cookie_path='Cookies/youtube.txt'), 201

    @app.get('/api/media')
    def media():
        config, base = settings()
        root = Path(io_path(video_directory(config, base)))
        return jsonify(**catalog.page(root, limit=int(request.args.get('limit', 24)),
                       cursor=request.args.get('cursor'), query=request.args.get('q', '')[:200]), directory=str(root),
                       indexing=catalog.root_status(root))

    @app.post('/api/media/reindex')
    def reindex_media():
        config, base = settings()
        task = store.create('index', {'config': config, 'base': str(base), 'scope': 'download'})
        return jsonify(task), 202

    @app.get('/api/overview')
    def overview():
        config, base = settings()
        root = video_directory(config, base)
        return jsonify(counts=store.counts(actions=('download', 'check', 'import', 'index')),
                       media=catalog.root_status(root), recent_media=catalog.page(root, limit=4)['items'],
                       recent_tasks=store.list(limit=5, actions=('download', 'check', 'import', 'index')))

    @app.get('/media/videos/<path:name>')
    def video(name):
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
        config, base = settings()
        root = Path(io_path(video_directory(config, base))).resolve()
        target = (root / name).resolve()
        if not target.is_relative_to(root) or target.suffix.lower() not in ('.jpg', '.jpeg', '.png', '.webp') or not target.is_file():
            return jsonify(error='封面不存在'), 404
        return send_file(target, conditional=True)

    @app.post('/api/media')
    def import_media():
        if store.pending():
            return jsonify(error='请等待当前任务结束后再导入素材。'), 409
        video, image = request.files.get('video'), request.files.get('cover')
        if video is None or image is None or not video.filename or not image.filename:
            raise ValueError('请选择一个视频和对应的封面图片。')
        suffix = Path(video.filename).suffix.lower()
        cover_suffix = Path(image.filename).suffix.lower()
        if suffix not in VIDEO_SUFFIXES or cover_suffix not in ('.jpg', '.jpeg', '.png', '.webp'):
            raise ValueError('视频支持 MP4 / MKV / WebM / MOV；封面支持 JPG / PNG / WebP。')
        stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', Path(video.filename).stem).strip(' .')[:100]
        if not stem or stem.upper() in ('CON', 'PRN', 'AUX', 'NUL', *(f'COM{i}' for i in range(10)), *(f'LPT{i}' for i in range(10))):
            raise ValueError('视频文件名无效，请重命名后重试。')
        config, base = settings()
        root = Path(io_path(video_directory(config, base)))
        root.mkdir(parents=True, exist_ok=True)
        target, cover_target = root / (stem + suffix), root / (stem + cover_suffix)
        with media_lock:
            if store.pending():
                return jsonify(error='请等待当前任务结束后再导入素材。'), 409
            if target.exists() or any((root / (stem + ext)).exists() for ext in ('.jpg', '.jpeg', '.webp', '.png')):
                return jsonify(error='存在同名素材，请先重命名文件。'), 409
            with tempfile.TemporaryDirectory(prefix='.import-', dir=root) as staging:
                temporary_video = Path(staging) / ('video' + suffix)
                temporary_cover = Path(staging) / ('cover' + cover_suffix)
                video.save(temporary_video)
                image.save(temporary_cover)
                if not temporary_video.stat().st_size:
                    raise ValueError('视频文件为空。')
                try:
                    with Image.open(temporary_cover) as opened:
                        opened.verify()
                except (UnidentifiedImageError, OSError, ValueError) as error:
                    raise ValueError('封面不是有效图片。') from error
                temporary_cover.replace(cover_target)
                try:
                    temporary_video.replace(target)
                except OSError:
                    cover_target.unlink(missing_ok=True)
                    raise
        catalog.index_file(target, root)
        return jsonify(message='素材已导入，封面已自动匹配视频名称。', name=target.name), 201

    @app.get('/api/tasks')
    def tasks():
        actions = ('download', 'check', 'import', 'index') if request.args.get('scope') == 'download' else None
        batch_id = request.args.get('batch_id')
        return jsonify(**store.page(actions=actions, limit=int(request.args.get('limit', 50)),
                       cursor=request.args.get('cursor'), status=request.args.get('status'),
                       query=request.args.get('q', '')[:200], batch_id=batch_id, order=request.args.get('order', 'desc')),
                       counts=store.counts(actions=actions, batch_id=batch_id))

    @app.get('/api/batches')
    def download_batches():
        return jsonify(store.batch_page(cursor=request.args.get('cursor')))

    @app.post('/api/downloads/import')
    def import_downloads():
        request.max_content_length = 1024 ** 3 + 1024 ** 2
        source = request.files.get('file')
        if not source or not source.filename or Path(source.filename).suffix.lower() not in ('.txt', '.csv'):
            raise ValueError('请选择 UTF-8 编码的 TXT 或 CSV 文件。')
        config, base = settings()
        config = {**DEFAULTS, **config}
        validate_settings(config, download_only=True)
        identity = uuid.uuid4().hex
        directory = store.directory / 'imports'
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / (identity + Path(source.filename).suffix.lower())
        payload = {'config': config, 'base': str(base), 'scope': 'download'}
        try:
            source.save(path)
            if not path.stat().st_size or path.stat().st_size > 1024 ** 3:
                raise ValueError('导入文件不能为空，最大 1 GB。')
            with media_lock:
                if config.get('proxy_mode') == 'clash':
                    payload['clash'] = clash.snapshot()
                from webapp.bulk import create_import
                task = create_import(store, identity, path, Path(source.filename).name, payload)
        except Exception:
            path.unlink(missing_ok=True)
            raise
        return jsonify(task), 202

    @app.get('/api/tasks/<task_id>')
    def task_detail(task_id):
        task = store.get(task_id)
        return (jsonify(task), 200) if task else (jsonify(error='任务不存在'), 404)

    @app.get('/api/tasks/<task_id>/logs')
    def task_logs(task_id):
        if not store.get(task_id):
            return jsonify(error='任务不存在'), 404
        try:
            offset = int(request.args.get('offset', 0))
        except ValueError:
            raise ValueError('日志偏移量必须为整数。')
        return jsonify(store.logs(task_id, offset))

    @app.post('/api/tasks')
    @app.post('/api/downloads')
    def create_task():
        data = body()
        batch = request.path == '/api/downloads'
        action = 'download' if batch else data.get('action')
        if action not in ACTIONS:
            raise ValueError('不支持的任务类型。')
        if action == 'publish' and data.get('confirmed') is not True:
            raise ValueError('发布会实际提交作品，请先确认。')
        config, base = settings()
        # Fill defaults in the snapshot only; opening the web app never rewrites YAML.
        config = {**DEFAULTS, **config}
        validate_settings(config, download_only=action == 'download' or (action == 'check' and data.get('scope') == 'download'))
        platforms = data.get('platforms', config['platforms']) if action in ('login', 'preview', 'prepare', 'publish') else []
        if action in ('login', 'preview', 'prepare', 'publish'):
            if not isinstance(platforms, list) or not platforms or any(p not in WEB_PLATFORMS for p in platforms):
                raise ValueError('Web 版当前支持抖音和快手；西瓜流程待适配。')
            platforms = list(dict.fromkeys(platforms))
        if action == 'login' and len(platforms) != 1:
            raise ValueError('请一次登录一个平台。')
        if action in ('preview', 'prepare', 'publish') and not all(shutil.which(tool) for tool in ('ffmpeg', 'ffprobe')):
            raise ValueError('缺少 FFmpeg / ffprobe，请安装后再预览、预处理或发布。')
        payload = {'config': config, 'base': str(base), 'platforms': platforms}
        if data.get('scope') == 'download':
            payload['scope'] = 'download'
        if action == 'download':
            payload['urls'] = urls_from(data.get('urls') or config.get('url'))
        with media_lock:
            if action == 'download' and config.get('proxy_mode') == 'clash':
                payload['clash'] = clash.snapshot()
            if batch:
                batch_id = uuid.uuid4().hex
                payloads = [{**payload, 'urls': [url], 'batch_id': batch_id} for url in payload['urls']]
                return jsonify(batch_id=batch_id, items=store.create_batch(payloads)), 202
            task = store.create(action, payload)
        return jsonify(task), 202

    register_clash(app, clash, store, media_lock, settings_lock, settings, config_path)

    if start_worker:
        def restore_clash():
            config, _ = settings()
            if config.get('proxy_mode') == 'clash':
                try:
                    clash.start()
                except ClashError as error:
                    clash.last_error = str(error)
        threading.Thread(target=restore_clash, name='restore-clash', daemon=True).start()
        runner = TaskRunner(store, lambda: settings()[0])
        runner.start()
        app.extensions['task_runner'] = runner
    return app


if __name__ == '__main__':
    if os.name == 'nt':
        from scripts.setup_database import ensure_running
        ensure_running()
    parser = argparse.ArgumentParser(description='视频工作台 Flask 服务')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', default=5000, type=int)
    parser.add_argument('--config', type=Path, default=DEFAULT_CONFIG)
    args = parser.parse_args()
    from waitress import serve
    print(f'视频工作台：http://{args.host}:{args.port}', flush=True)
    serve(create_app(args.config), host=args.host, port=args.port, threads=8,
          max_request_body_size=8 * 1024 ** 3, channel_timeout=300)
