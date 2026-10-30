"""Optional virtual display for native browser resolution in the Linux app."""
import errno
import os
from pathlib import Path
import socket
import subprocess
import threading
import time

import psutil

from webapp.deployment_errors import BrowserDesktopError


def prepare_display(directory):
    """Remove :99 artifacts only after ruling out a live socket and X server.

    Docker restarts retain /tmp while assigning new PIDs, so an old lock PID
    may now belong to an unrelated process in the new container.
    """
    endpoint = directory / '.X11-unix' / 'X99'
    lock = directory / '.X99-lock'
    try:
        with socket.socket(socket.AF_UNIX) as connection:
            connection.settimeout(1)
            connection.connect(str(endpoint))
    except OSError as error:
        if error.errno not in (errno.ENOENT, errno.ECONNREFUSED):
            raise  # A timeout or permission error does not prove it is stale.
    else:
        raise BrowserDesktopError('Xvfb', 'Display :99 is already in use')

    try:
        pid = int(lock.read_text(encoding='ascii').strip())
        if pid <= 0:
            raise ValueError
    except FileNotFoundError:
        pid = None
    except (ValueError, UnicodeError):
        raise BrowserDesktopError('Xvfb', 'Invalid /tmp/.X99-lock; check the display owner') from None
    if pid is not None:
        try:
            command = psutil.Process(pid).cmdline()
        except psutil.NoSuchProcess:
            command = None
        except psutil.AccessDenied:
            raise BrowserDesktopError('Xvfb', 'Cannot verify /tmp/.X99-lock owner') from None
        if command is not None and (not command or Path(command[0]).name.lower() in
                                    {'xvfb', 'xorg', 'x', 'xwayland', 'xephyr', 'xnest'}):
            raise BrowserDesktopError('Xvfb', 'Display :99 lock belongs to a live or unverified X server')
    endpoint.unlink(missing_ok=True)
    lock.unlink(missing_ok=True)


def start_logged(command, path):
    from webapp.maintenance import BoundedLog
    path.touch(exist_ok=True)
    writer = BoundedLog(path, 2 * 1024 ** 2)
    try:
        child = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    except FileNotFoundError:
        raise BrowserDesktopError(command[0], 'Required desktop program is not installed') from None
    def drain():
        with child.stdout as stream:
            while chunk := stream.read1(65536):
                writer.write(chunk)
    threading.Thread(target=drain, name='browser-desktop-log', daemon=True).start()
    return child


def start_desktop():
    os.environ.update(DISPLAY=':99', YT5S_BROWSER='/usr/bin/chromium', YT5S_CONTAINER_BROWSER='1')
    children = []
    try:
        prepare_display(Path('/tmp'))
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
                    raise BrowserDesktopError('Xvfb', 'Virtual display startup failed; see /tmp/xvfb.log',
                                              exit_code=children[0].poll()) from None
                time.sleep(0.1)
        for name, command in [
            ('x11vnc', ['x11vnc', '-display', ':99', '-localhost', '-forever', '-shared', '-nopw', '-rfbport', '5900']),
            ('novnc', ['websockify', '--web=/usr/share/novnc', '6080', '127.0.0.1:5900']),
        ]:
            children.append(start_logged(command, Path('/tmp') / (name + '.log')))
        time.sleep(0.2)
        for name, child in zip(('Xvfb', 'x11vnc', 'novnc'), children):
            code = child.poll()
            if code is not None:
                raise BrowserDesktopError(name, f'Browser desktop startup failed; see /tmp/{name.lower()}.log',
                                          exit_code=code)
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
