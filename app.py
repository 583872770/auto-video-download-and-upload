"""Compatibility entrypoint for the Flask console and application factory."""
if __name__ == '__main__':
    print('正在加载视频工作台…', flush=True)

from webapp.settings import save_settings
from webapp.yt5s import require_browser_environment
from webapp.environment import require_fallback_environment, require_downmp3_environment
from webapp.yt1d import require_environment as require_yt1d_environment


def create_app(config_path=None, state_dir=None, *, start_worker=True, env_file=None,
               role=None, instance_id=None, instance_url='', instance_name=None, database_url=None,
               startup_report=None):
    from webapp.application import build_application
    from webapp.services.context import WebDependencies
    from webapp import fleet_routes
    # Resolve callbacks at invocation time so embedded users can override adapters.
    dependencies = WebDependencies(
        save_settings=lambda *args, **kwargs: save_settings(*args, **kwargs),
        require_browser_environment=lambda *args, **kwargs: require_browser_environment(*args, **kwargs),
        require_fallback_environment=lambda *args, **kwargs: require_fallback_environment(*args, **kwargs),
        require_downmp3_environment=lambda *args, **kwargs: require_downmp3_environment(*args, **kwargs),
        require_yt1d_environment=lambda *args, **kwargs: require_yt1d_environment(*args, **kwargs),
        selected_id=lambda: fleet_routes.selected_id(),
        rpc=lambda *args, **kwargs: fleet_routes.rpc(*args, **kwargs),
        preflight_instances=lambda *args, **kwargs: fleet_routes.preflight_instances(*args, **kwargs))
    return build_application(__name__, config_path, state_dir, start_worker=start_worker, env_file=env_file,
                             role=role, instance_id=instance_id, instance_url=instance_url,
                             instance_name=instance_name, database_url=database_url,
                             startup_report=startup_report, dependencies=dependencies)


if __name__ == '__main__':
    from webapp.cli import main
    main()
