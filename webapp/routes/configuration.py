"""Configuration HTTP endpoints."""
from webapp.helpers import get_body, json_result


def register_configuration_routes(context, configuration):
    app = context.app
    body = get_body

    @app.get('/api/config')
    def read_config():
        return json_result(configuration.read_config())

    @app.put('/api/config')
    def update_config():
        return json_result(configuration.update_config(body()))
