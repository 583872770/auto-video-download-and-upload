"""Instance-local bounds for disk-heavy work, independent of source admission."""
from contextlib import contextmanager
from pathlib import Path
import time

from webapp.disk_budget import file_lock
from webapp.diagnostics import event


@contextmanager
def local_processing(directory, config, progress, operation, *, size=0, resource='local-processing', limit=None):
    limit = max(1, min(8, int(limit or config.get('postprocess_limit', 2))))
    started = time.monotonic()
    acquired = None
    try:
        while acquired is None:
            for slot in range(limit):
                candidate = file_lock(Path(directory) / resource / f'{slot}.lock')
                if candidate.__enter__():
                    acquired = candidate
                    break
                candidate.__exit__(None, None, None)
            if acquired is None:
                # Reporting checks the task fence and disk budget. Existing
                # worker heartbeats remain active while waiting for local work.
                progress.state.update(stage='waiting_processing', speed=None, eta=None,
                    local_operation=operation)
                progress.save()
                time.sleep(.25)
        admitted = time.monotonic()
        progress.state.update(stage='processing', speed=None, eta=None, local_operation=operation)
        progress.save(force=True)
        outcome = 'failed'
        try:
            yield
            outcome = 'completed'
        finally:
            elapsed = time.monotonic() - admitted
            values = dict(progress.state.get('local_processing_seconds') or {})
            values[operation] = round(values.get(operation, 0) + elapsed, 3)
            progress.state.update(local_processing_seconds=values, local_operation=None)
            event('local_processing_end', operation=operation, outcome=outcome,
                elapsed_seconds=round(elapsed, 3), wait_seconds=round(admitted-started, 3),
                file_bytes=size, local_limit=limit)
    finally:
        if acquired:
            acquired.__exit__(None, None, None)
