"""Compatibility TaskStore facade composed from explicit repository contracts."""
import json
import os
import socket
import uuid

from webapp.database import Database
from webapp.store_utils import now, source_identity

from webapp.repositories.task_codec import TaskCodec
from webapp.repositories.task_writes import TaskWrites
from webapp.repositories.task_queries import TaskQueries
from webapp.repositories.queue_control import QueueControl
from webapp.repositories.task_leases import TaskLeases
from webapp.repositories.task_history import TaskHistory


class TaskStore(TaskCodec, TaskWrites, TaskQueries, QueueControl, TaskLeases, TaskHistory):
    def __init__(self, directory, database_url=None, *, startup_report=None):
        self.db = Database(directory, database_url, startup_report=startup_report)
        self.directory = self.db.directory
        self.database = self.directory / 'tasks.sqlite3'
        self.owner = f'{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:12]}'
        self.fleet = None
        from webapp.progress_cache import ProgressCache
        self.progress_cache = ProgressCache(self.db.url)
        self._progress_checkpoints = {}
        from webapp.modes import single_state
        self.single = single_state(self.directory)
        runtime = self.directory / 'fleet-runtime.json'
        if runtime.is_file():
            from webapp.fleet import Fleet
            saved = json.loads(runtime.read_text(encoding='utf-8'))
            fleet = Fleet(self, saved['role'], identity=saved['id'])
            fleet.boot_id = saved['boot_id']
        from webapp.statistics import DownloadStatistics
        self.statistics = DownloadStatistics(self)
        from webapp.lists import DownloadLists
        self.lists = DownloadLists(self)


    def connect(self):
        return self.db.engine.begin()


    def _timestamp(self):
        return now()
