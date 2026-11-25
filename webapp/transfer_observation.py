"""Observe native file boundaries without changing sleep, retries or admission."""
import time


class FileTransferObservation:
    def __init__(self, session, fd, filename):
        self.session = session
        self.progress = session.progress
        self.totals = dict(self.progress.state.get('network_seconds') or {})
        self.phase = None
        self.started = None
        self.bytes = fd.filesize_or_none(fd.temp_name(filename)) or 0
        self.fd = fd
        self.had_override = 'real_download' in vars(fd)
        self.original_override = vars(fd).get('real_download')
        self.real_download = fd.real_download
        fd.real_download = self.request
        fd.add_progress_hook(self.hook)

    def transition(self, phase, *, until=None, publish=True):
        now = time.monotonic()
        if self.phase and self.started is not None:
            elapsed = max(0, now - self.started)
            self.totals[self.phase] = self.totals.get(self.phase, 0) + elapsed
            if self.phase == 'transferring':
                self.session.transfer_seconds += elapsed
        self.phase, self.started = phase, now
        stage = {'interval': 'waiting_request', 'connecting': 'waiting_connection',
                 'transferring': 'downloading'}.get(phase)
        self.progress.state.update(network_phase=phase, network_phase_started_at=time.time() if phase else None,
            network_seconds={key: round(value, 3) for key, value in self.totals.items()},
            request_wait_until=until, speed=None, eta=None)
        if stage:
            self.progress.state['stage'] = stage
        if publish:
            self.progress.save(force=True)

    def sleeping(self, seconds):
        self.transition('interval', until=time.time() + seconds if seconds is not None else None)

    def request(self, filename, info):
        # FileDownloader calls this only AFTER its native sleep and file checks.
        self.transition('connecting')
        return self.real_download(filename, info)

    def hook(self, event):
        downloaded = event.get('downloaded_bytes') or 0
        if event.get('status') == 'downloading' and downloaded > self.bytes:
            if self.phase != 'transferring':
                self.transition('transferring')
        self.bytes = downloaded
        if event.get('status') == 'finished' and self.phase:
            self.transition(None)

    def close(self, *, publish=True):
        if self.phase:
            self.transition(None, publish=publish)

    def detach(self):
        """Do not retain one downloader/session cycle for every caption file."""
        if self.had_override:
            self.fd.real_download = self.original_override
        else:
            del self.fd.real_download
        self.fd._progress_hooks.remove(self.hook)
        self.fd = self.real_download = self.original_override = None
        self.session = self.progress = None

