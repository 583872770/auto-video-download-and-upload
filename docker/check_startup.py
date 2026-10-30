"""Verify the packaged application without MySQL, workers, or persistent state."""
from pathlib import Path
import os
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import create_app


def main():
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith(('VIDEO_', 'MYSQL_', 'YT5S_'))}
    with patch.dict(os.environ, environment, clear=True), \
            tempfile.TemporaryDirectory(prefix='video-startup-check-') as temporary:
        root = Path(temporary)
        config = root / 'config.yaml'
        config.write_text('path: videos\nproxy_mode: direct\n', encoding='utf-8')
        for registered in (False, True):
            options = {'role': 'hybrid', 'instance_id': 'startup-check'} if registered else {}
            app = create_app(config, root / str(registered), start_worker=False, **options)
            try:
                app.testing = True
                client = app.test_client()
                health = client.get('/health')
                assert health.status_code == 200 and health.json == {'status': 'ok', 'role': 'hybrid'}
                assert client.get('/').status_code == 200
                assert client.get('/api/disk').status_code == 200
                assert client.get('/api/media').status_code == 200
                assert client.get('/api/status').status_code == 200
                assert client.get('/api/downloads/startup').status_code == 200
                assert (app.extensions['fleet'] is not None) == registered
                with client.session_transaction() as session:
                    csrf = session['csrf']
                assert client.put('/api/config', json={'worker_count': 1},
                                  headers={'X-CSRF-Token': csrf}).status_code == 200
            finally:
                app.extensions['task_store'].db.engine.dispose()
    print('Packaged application startup verified.')


if __name__ == '__main__':
    main()
