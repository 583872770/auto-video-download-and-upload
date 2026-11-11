"""Authentication, response policy and shared HTTP error handling."""
import secrets
from flask import g, jsonify, request, session
from werkzeug.exceptions import HTTPException
from webapp.youtube_auth import YouTubeAuthRequired
from webapp.yt5s import BrowserEnvironmentRequired
from webapp.environment import DownloadEnvironmentRequired
from webapp.youtube_login import LoginBusy
from webapp.throughput import PipelineBusy
from webapp.disk_budget import DiskPressure
from sqlalchemy.exc import SQLAlchemyError


def register_middleware(context):
    app = context.app
    console_auth = context.console_auth
    fleet = context.fleet
    settings = context.settings
    store = context.store
    youtube_auth = context.youtube_auth

    @app.before_request
    def csrf_protection():
        from webapp.console_auth import rpc_allowed
        g.fleet_rpc = False
        if fleet and fleet.can_download:
            if request.path == '/health' and request.method == 'GET':
                return None
            token = request.headers.get('Authorization', '').removeprefix('Bearer ')
            if token and secrets.compare_digest(token, fleet.token):
                if not rpc_allowed():
                    return jsonify(error='节点凭据不能用于管理客户任务、账单或账户。'), 403
                g.fleet_rpc = True
                return None
            if request.headers.get('Authorization'):
                return jsonify(error='实例认证凭据无效。'), 403
        if 'csrf' not in session:
            session['csrf'] = secrets.token_urlsafe(32)
        if request.method not in ('GET', 'HEAD', 'OPTIONS'):
            token = request.headers.get('X-CSRF-Token', '')
            if not secrets.compare_digest(token, session['csrf']):
                return jsonify(error='页面会话已失效，请刷新后重试。'), 403
        return console_auth.guard()

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
            if request.method in {'POST', 'PUT', 'PATCH', 'DELETE'} and 200 <= response.status_code < 300:
                runner = app.extensions.get('task_runner')
                if runner:
                    runner.wake()
        elif request.path.startswith('/static/'):
            response.headers['Cache-Control'] = 'no-cache, must-revalidate'
        return response

    @app.errorhandler(DiskPressure)
    def disk_pressure(error):
        return jsonify(error=str(error), error_type='disk_wait'), 507

    @app.errorhandler(ValueError)
    def invalid(error):
        return jsonify(error=str(error)), 400

    @app.errorhandler(OSError)
    def filesystem_error(error):
        from webapp.disk_budget import is_disk_full
        if is_disk_full(error):
            return jsonify(error='磁盘空间或配额不足，请等待自动清理或释放空间。', error_type='disk_wait'), 507
        app.logger.exception('文件操作失败')
        return jsonify(error=f'文件操作失败：{error}'), 400

    @app.errorhandler(HTTPException)
    def http_error(error):
        return jsonify(error=error.description), error.code

    @app.errorhandler(SQLAlchemyError)
    def database_error(error):
        app.logger.exception('数据库操作失败')
        return jsonify(error='数据库暂时不可用，请检查数据库服务。'), 503

    @app.errorhandler(LoginBusy)
    def login_busy(error):
        return jsonify(error=str(error)), 409

    @app.errorhandler(PipelineBusy)
    def pipeline_busy(error):
        return jsonify(error=str(error), retry_after=getattr(error, 'retry_after', 10)), 409

    @app.errorhandler(YouTubeAuthRequired)
    def youtube_auth_required(error):
        from webapp.download_policy import failure_evidence, recovery_policy
        failure = failure_evidence(error)
        policy = recovery_policy(failure, settings()[0])
        if policy.targets == ('session',):
            return jsonify(error=str(error), auth=error.result, failure=failure.public(),
                           message='仅该账号需要人工处理登录或验证，其他健康账号继续。' if policy.manual else
                                   '仅该账号请求冷却，其他健康账号继续，实例未暂停。'), 409
        youtube_auth.block(error.result)
        return jsonify(error=str(error), auth=error.result, download_queue=store.download_queue()), 409

    @app.errorhandler(BrowserEnvironmentRequired)
    @app.errorhandler(DownloadEnvironmentRequired)
    def browser_environment_required(error):
        return jsonify(error=str(error), download_queue=store.download_queue()), 409
