"""Video and caption outputs contend for the same leased transfer resources."""
from contextlib import contextmanager, ExitStack
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from sqlalchemy import select

from webapp.adapters.execution import ProviderExecution
from webapp.channel_scheduler import ChannelScheduler
from webapp.database import download_permits, tasks
from webapp.download_metrics import DownloadMeter
from webapp.downloads import DownloadLeaseLost, DownloadProgress
from webapp.store import TaskStore
from webapp.throughput import PipelineBusy


class SharedOutputAdmissionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.now = time.time()
        self.url = 'sqlite:///' + (self.root / 'shared.db').as_posix()
        browsers = patch('webapp.yt5s_browser.close_all')
        browsers.start()
        self.addCleanup(browsers.stop)

    def executor(self, identity, kind, limit=2):
        store = TaskStore(self.root / identity, self.url)
        self.addCleanup(store.db.engine.dispose)
        task = store.create('download', {'urls': ['https://youtu.be/jNQXAC9IVRw']})
        with store.connect() as db:
            db.execute(tasks.update().where(tasks.c.id == task['id']).values(
                status='running', worker_id=store.owner, lease_until=self.now + 86400))
        config = dict(download_method='yt_dlp', throughput_mode='manual', worker_count=limit,
                      builtin_transfer_limit=limit, download_connection_limit=limit,
                      youtube_start_interval=11)
        router = ChannelScheduler(store, config, task['id'])
        router.clock = lambda db: self.now
        reserve = router.permit

        @contextmanager
        def immediate(*args, **kwargs):
            kwargs['timeout'] = 0
            with reserve(*args, **kwargs) as actual:
                yield actual

        router.permit = immediate
        progress = DownloadProgress(lambda value: None)
        progress.state['output_kind'] = kind
        meter = DownloadMeter(store.directory)
        meter.begin(router.channels[0])
        session = SimpleNamespace(base=self.root, store=store, config=config, scheduler=router,
            progress=progress, meter=meter, transfer_seconds=0, failure_phase='resolve')
        return ProviderExecution(session)

    def transfer(self, executor):
        router = executor.session.scheduler
        return executor.transmitting(router.channels[0], {}, 1)

    def test_video_and_subtitles_share_the_configured_total_not_separate_caps(self):
        video = self.executor('video', 'video_only')
        captions = self.executor('captions', 'subtitle')
        others = [self.executor('next-video', 'video_only'), self.executor('next-caption', 'subtitle')]
        with self.transfer(video), self.transfer(captions):
            router = video.session.scheduler
            scopes = ['local-transfers:local', 'local-connections:local', 'upstream:youtube-origin']
            _, active, _ = router.snapshot(scopes)
            self.assertEqual({scope: active[scope] for scope in scopes}, dict.fromkeys(scopes, 2))
            for executor in others:
                with self.subTest(kind=executor.session.progress.state['output_kind']), self.assertRaises(PipelineBusy):
                    with self.transfer(executor):
                        self.fail('Total output limit exceeded')
                self.assertEqual(executor.session.progress.state['stage'], 'waiting_transfer')
        for executor in others:
            with self.transfer(executor):
                self.assertEqual(executor.session.progress.state['stage'], 'downloading')
        with video.session.store.connect() as db:
            self.assertEqual(db.execute(select(download_permits)).all(), [])

    def test_configured_capacity_allows_multiple_caption_requests(self):
        executors = [self.executor(str(index), 'subtitle', limit=3) for index in range(3)]
        with ExitStack() as stack:
            for executor in executors:
                stack.enter_context(self.transfer(executor))
            _, active, _ = executors[0].session.scheduler.snapshot(['local-transfers:local'])
            self.assertEqual(active['local-transfers:local'], 3)

    def test_video_and_subtitle_tasks_observe_the_same_start_deadline(self):
        video = self.executor('video', 'video_only').session.scheduler
        captions = self.executor('captions', 'subtitle').session.scheduler
        video.wait_youtube_start(video.channels[0])

        def waiting():
            raise PipelineBusy('waiting on common start interval')

        with self.assertRaisesRegex(PipelineBusy, 'common start interval'):
            captions.wait_youtube_start(captions.channels[0], on_wait=waiting)
        self.now += 12
        captions.wait_youtube_start(captions.channels[0])
        state = captions.snapshot(['youtube-start:local'])[0]['youtube-start:local']
        self.assertEqual(state['next_at'], self.now + 11)

    def test_all_output_transfers_classify_early_failures_and_release_capacity(self):
        for kind in ('video_only', 'subtitle'):
            executor = self.executor(kind, kind)
            with self.assertRaisesRegex(RuntimeError, 'before first progress'):
                with self.transfer(executor):
                    self.assertEqual(executor.session.failure_phase, 'transfer')
                    self.assertEqual(executor.session.progress.state['stage'], 'downloading')
                    raise RuntimeError('before first progress')
            with executor.session.store.connect() as db:
                self.assertEqual(db.execute(select(download_permits)).all(), [])

    def test_subtitle_cannot_acquire_shared_capacity_after_losing_its_lease(self):
        executor = self.executor('captions', 'subtitle')
        router = executor.session.scheduler
        with executor.session.store.connect() as db:
            db.execute(tasks.update().where(tasks.c.id == router.task_id).values(worker_id='replacement'))
        with self.assertRaises(DownloadLeaseLost):
            with self.transfer(executor):
                self.fail('A stale caption owner acquired capacity')

    def test_common_meter_counts_actual_caption_and_video_bytes_without_duplicates(self):
        meter = self.executor('meter', 'subtitle').session.meter
        for filename, extension, size in (('video.mp4', 'mp4', 1000), ('caption.vtt', 'vtt', 50)):
            for status in ('downloading', 'finished'):
                meter.hook(dict(filename=filename, downloaded_bytes=size, status=status,
                                info_dict={'ext': extension}))
        self.assertEqual(meter.data['received_bytes'], 1050)
        self.assertEqual(meter.data['groups']['youtube-origin']['received_bytes'], 1050)
