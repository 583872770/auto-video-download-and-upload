"""Cross-process browser capacity; OS locks are released if a worker is killed."""
from contextlib import contextmanager, ExitStack
from pathlib import Path
import time


@contextmanager
def browser_slot(directory, capacity, on_wait=None, *, preferred=0, timeout=600):
    from web_worker import worker_lock

    directory = Path(directory) / 'browser-slots'
    directory.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    waiting = False
    while True:
        with ExitStack() as selected:
            slot = None
            # Serialize admission only. Browser operations run outside this lock.
            with worker_lock(directory / 'admission.lock') as admitted:
                if admitted:
                    limit = capacity() if callable(capacity) else capacity
                    if type(limit) is not int or not 1 <= limit <= 16:
                        raise ValueError('浏览器并发数必须是 1 到 16 之间的整数。')
                    busy, free = 0, []
                    for index in range(16):
                        with worker_lock(directory / f'slot-{index}.lock') as available:
                            if available:
                                free.append(index)
                            else:
                                busy += 1
                    # Count all slots so lowering the limit lets current browsers
                    # finish before admitting replacements, including high slots.
                    if busy < limit:
                        candidate = min(free, key=lambda index: (index - preferred) % 16)
                        if selected.enter_context(worker_lock(directory / f'slot-{candidate}.lock')):
                            slot = candidate
            if slot is not None:
                yield slot
                return
        if time.monotonic() >= deadline:
            raise RuntimeError('等待 yt5s 浏览器名额超时，请稍后重试或调整浏览器并发数。')
        if not waiting and on_wait:
            on_wait()
            waiting = True
        time.sleep(0.5)
