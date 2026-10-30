"""Route blueprints."""
from flask import Flask


def register_blueprints(app: Flask):
    """Register all route blueprints."""
    from . import health, disk

    app.register_blueprint(health.bp)
    app.register_blueprint(disk.bp)
