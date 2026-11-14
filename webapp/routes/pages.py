"""Pages HTTP endpoints."""
from flask import redirect, render_template, session, request


def register_pages_routes(context):
    app = context.app
    service_role = context.service_role

    @app.get('/')
    def index():
        from webapp.frontend import render_workspace
        workspace = render_workspace(app)
        if workspace is not None:
            return workspace
        from webapp.cobalt_catalog import catalog
        from webapp.risk_diagnostics import GUIDE
        return render_template('index.html', csrf=session['csrf'], service_role=service_role, cobalt_catalog=catalog(), risk_guide=GUIDE)

    @app.get('/settings')
    def settings_page():
        from webapp.frontend import render_workspace
        return render_workspace(app) or redirect('/#settings')

    @app.get('/workbench')
    @app.get('/media')
    @app.get('/logs')
    @app.get('/accounts')
    @app.get('/instances')
    @app.get('/statistics')
    @app.get('/proxy')
    @app.get('/deployment')
    def execution_page():
        from webapp.frontend import render_workspace
        view = {'/workbench': 'overview', '/media': 'media', '/logs': 'tasks', '/accounts':'accounts', '/instances':'instances', '/statistics':'statistics', '/proxy':'clash', '/deployment':'deployment'}[request.path]
        query = request.query_string.decode('utf-8', errors='replace')
        return render_workspace(app) or redirect('/' + ('?' + query if query else '') + '#' + view)
