"""Pages HTTP endpoints."""
from flask import render_template, session


def register_pages_routes(context):
    app = context.app
    service_role = context.service_role

    @app.get('/')
    def index():
        from webapp.cobalt_catalog import catalog
        from webapp.risk_diagnostics import GUIDE
        return render_template('index.html', csrf=session['csrf'], service_role=service_role, cobalt_catalog=catalog(), risk_guide=GUIDE)
