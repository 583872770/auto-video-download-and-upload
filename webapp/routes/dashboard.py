"""Dashboard HTTP endpoints."""
from webapp.helpers import json_result


def register_dashboard_routes(context, dashboard):
    app = context.app

    @app.get('/api/status')
    def status():
        return json_result(dashboard.status())

    @app.get('/api/overview')
    def overview():
        return json_result(dashboard.overview())
