"""Pages HTTP endpoints."""
from flask import g, redirect, render_template, session


def register_pages_routes(context):
    app = context.app
    service_role = context.service_role

    @app.get('/')
    def index():
        from webapp.cobalt_catalog import catalog
        from webapp.risk_diagnostics import GUIDE
        return render_template('index.html', csrf=session['csrf'], service_role=service_role, cobalt_catalog=catalog(), risk_guide=GUIDE)

    @app.get('/settings')
    def settings_page():
        from webapp.frontend import assets
        from webapp.cobalt_catalog import catalog
        bundle = assets(app.static_folder)
        if bundle is None:
            return redirect('/#settings')
        return render_template('frontend.html', assets=bundle, bootstrap={
            'csrf': session['csrf'], 'fleetEnabled': context.fleet is not None,
            'role': g.console_user['role'] if g.console_user else 'admin',
            'cobaltCatalog': catalog(),
        })
