"""Compatibility entrypoint for queued worker execution."""
import os
import time
import traceback

from webapp.tasks import TaskStore
from webapp.worker.formatting import format_bytes
from webapp.worker.locks import worker_lock


def _execute(action, payload, report_progress=None, rotation_completed=None, store=None, worker_slot=0):
    from webapp.worker.actions import run_action
    return run_action(action, payload, report_progress, rotation_completed, store, worker_slot)


def execute(action, payload, report_progress=None, rotation_completed=None, store=None, worker_slot=0,
            task_id=None, start_ticket=None):
    from webapp.worker.workspace import execute_task
    return execute_task(action, payload, report_progress, rotation_completed, store, worker_slot,
                        task_id, start_ticket, pipeline=_execute)


def drain(directory, slot=0, role='all'):
    from webapp.worker.queue import drain_queue
    return drain_queue(directory, slot, role, execute_task=execute, store_factory=TaskStore)


if __name__ == '__main__':
    from webapp.worker.cli import main
    main(drain)
