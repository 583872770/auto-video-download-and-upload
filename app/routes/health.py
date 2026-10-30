"""Health check and basic status routes."""
from flask import Blueprint, current_app, jsonify

bp = Blueprint('health', __name__)


@bp.get('/health')
def health():
    """Database health check."""
    store = current_app.extensions['task_store']
    with store.connect() as db:
        db.exec_driver_sql('SELECT 1')

    service_role = current_app.config.get('SERVICE_ROLE')
    return jsonify(status='ok', role=service_role)
