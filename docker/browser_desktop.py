"""Optional virtual display for native browser resolution in the Linux app."""
import os
from pathlib import Path
import socket
import subprocess
import threading
import time


def start_logged(command, path):
    from webapp.maintenance import BoundedLog
    path.touch(exist_ok=True)
    writer = BoundedLog(path, 2 * 1024 ** 2)
    child = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    def drain():
        with child.stdout as stream:
            while chunk := stream.read(65536):
                writer.write(chunk)
    threading.Thread(target=drain, name='browser-desktop-log', daemon=True).start()
    return child


def start_desktop():
    os.environ.update(DISPLAY=':99', YT5S_BROWSER='/usr/bin/chromium', YT5S_CONTAINER_BROWSER='1')
    children = []
    try:
        children.append(start_logged(['Xvfb', ':99', '-screen', '0', '1280x900x24', '-nolisten', 'tcp'],
                                     Path('/tmp') / 'xvfb.log'))
        deadline = time.monotonic() + 10
        while True:
            try:
                with socket.socket(socket.AF_UNIX) as connection:
                    connection.connect('/tmp/.X11-unix/X99')
                break
            except OSError:
                if children[0].poll() is not None or time.monotonic() >= deadline:
                    raise OSError('Virtual display startup failed')
                time.sleep(0.1)
        for name, command in [
            ('x11vnc', ['x11vnc', '-display', ':99', '-localhost', '-forever', '-shared', '-nopw', '-rfbport', '5900']),
            ('novnc', ['websockify', '--web=/usr/share/novnc', '6080', '127.0.0.1:5900']),
        ]:
            children.append(start_logged(command, Path('/tmp') / (name + '.log')))
        time.sleep(0.2)
        if any(child.poll() is not None for child in children):
            raise OSError('Browser desktop startup failed')
    except BaseException:
        for child in children:
            if child.poll() is None:
                child.terminate()
        for child in children:
            try:
                child.wait(timeout=3)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=3)
        raise
