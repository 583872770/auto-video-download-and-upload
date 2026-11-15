"""Authenticated entry points for the current Vue workspace."""
from webapp.frontend import render_workspace


def register_pages_routes(context):
    app = context.app

    @app.get('/')
    @app.get('/settings')
    @app.get('/workbench')
    @app.get('/media')
    @app.get('/logs')
    @app.get('/accounts')
    @app.get('/instances')
    @app.get('/statistics')
    @app.get('/proxy')
    @app.get('/deployment')
    def workspace_page():
        return render_workspace(app)
