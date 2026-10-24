"""Optional virtual display for native browser resolution in the Linux app."""
import os
from pathlib import Path
import socket
import subprocess
import time


def start_desktop():
    os.environ.update(DISPLAY=':99', YT5S_BROWSER='/usr/bin/chromium', YT5S_CONTAINER_BROWSER='1')
    children = []
    try:
        children.append(subprocess.Popen(['Xvfb', ':99', '-screen', '0', '1280x900x24', '-nolisten', 'tcp'],
                                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
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
            # Keep these children alive across the entrypoint's exec into Flask.
            with (Path('/tmp') / (name + '.log')).open('ab') as log:
                children.append(subprocess.Popen(command, stdout=log, stderr=log))
        time.sleep(0.2)
        if any(child.poll() is not None for child in children):
            raise OSError('Browser desktop startup failed')
    except BaseException:
        for child in children:
            if child.poll() is None:
                child.terminate()
        raise
