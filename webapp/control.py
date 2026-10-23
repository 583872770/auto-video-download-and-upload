"""Stop this queue's local download executors, including media subprocesses."""
from pathlib import Path
import os
import socket

import psutil

ROOT = Path(__file__).resolve().parents[1]


def terminate_worker(owner, directory):
    if not owner:
        return False
    try:
        host, pid, identity = owner.rsplit(':', 2)
        if host != socket.gethostname() or not identity:
            return False
        process = psutil.Process(int(pid))
        args = process.cmdline()
        script = next((i for i, arg in enumerate(args[1:], 1) if Path(arg).resolve() == ROOT / 'web_worker.py'), None)
        if script is None or any(arg not in ('-u', '-B') for arg in args[1:script]):
            return False
        position = args.index('--state')
        if Path(args[position + 1]).resolve() != Path(directory).resolve() or Path(process.cwd()).resolve() != ROOT:
            return False
        # Mihomo is persistent infrastructure. Keep proxy selection and rotation state.
        proxy = (ROOT / '.tools/mihomo' / ('mihomo.exe' if os.name == 'nt' else 'mihomo')).resolve()
        def descendants():
            result = []
            for child in process.children(recursive=True):
                try:
                    if Path(child.exe()).resolve() != proxy:
                        result.append(child)
                except psutil.NoSuchProcess:
                    continue
            return result

        children = descendants()
        # Stop the parent first so it cannot spawn another FFmpeg/JS process.
        process.suspend()
        killed = False
        try:
            children.extend(child for child in descendants() if child not in children)
            for child in reversed(children):
                try:
                    child.kill()
                except psutil.NoSuchProcess:
                    pass
            process.kill()
            killed = True
        finally:
            if not killed:
                try:
                    process.resume()
                except psutil.Error:
                    pass
        _, alive = psutil.wait_procs([process, *children], timeout=5)
        return not alive
    except psutil.NoSuchProcess:
        return True
    except (psutil.Error, ValueError, IndexError, OSError):
        return False


def stop_downloads(store):
    return store.stop_running_downloads(lambda owner: terminate_worker(owner, store.directory))
