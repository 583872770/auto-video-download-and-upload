"""Dashboard HTTP endpoints."""
from flask import jsonify, request
from webapp.helpers import json_result
from webapp.workbench_live import QUEUE, live_snapshot, queue_details


def register_dashboard_routes(context, dashboard):
    app = context.app

    @app.get('/api/status')
    def status():
        return json_result(dashboard.status())

    def workbench_instance():
        fleet = context.fleet
        if not fleet:
            return 'local'
        if fleet.can_manage:
            from webapp.fleet_routes import selected_id
            return selected_id()
        return fleet.id

    @app.get('/api/workbench/live')
    def workbench_live():
        value = live_snapshot(context.store.progress_cache, workbench_instance())
        if value is None:
            return jsonify(error='实时状态暂不可用，等待实例重新上报。'), 503
        return jsonify(value)

    @app.get('/api/workbench/queue')
    def workbench_queue():
        kind = request.args.get('kind')
        cursor = request.args.get('cursor', '0')
        if kind not in QUEUE or not cursor.isascii() or not cursor.isdecimal() or len(cursor) > 18:
            raise ValueError('无效的队列分类或分页位置。')
        return jsonify(queue_details(context.store, workbench_instance(), kind, int(cursor)))
