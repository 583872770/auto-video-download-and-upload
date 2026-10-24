#!/bin/sh
set -eu

desktop_pid=''
vnc_pid=''
web_pid=''
probe_pid=''
cleanup() {
    for child in "$probe_pid" "$web_pid" "$vnc_pid" "$desktop_pid"; do
        if [ -n "$child" ]; then kill "$child" 2>/dev/null || true; fi
    done
}
trap cleanup EXIT
trap 'exit 143' TERM
trap 'exit 130' INT

Xvfb :99 -screen 0 1280x900x24 -nolisten tcp &
desktop_pid=$!
python - <<'PY'
import socket
import time
deadline = time.monotonic() + 10
while True:
    try:
        with socket.socket(socket.AF_UNIX) as connection:
            connection.connect('/tmp/.X11-unix/X99')
        break
    except OSError:
        if time.monotonic() >= deadline:
            raise RuntimeError('虚拟显示启动失败')
        time.sleep(0.1)
PY
x11vnc -display :99 -localhost -forever -shared -nopw -rfbport 5900 >/tmp/x11vnc.log 2>&1 &
vnc_pid=$!
websockify --web=/usr/share/novnc 6080 127.0.0.1:5900 >/tmp/novnc.log 2>&1 &
web_pid=$!
printf '%s\n' '远程桌面已启动；请通过 SSH 隧道访问 http://127.0.0.1:6080/vnc.html?autoconnect=true'
python /opt/yt5s/scripts/probe_yt5s.py --output /results &
probe_pid=$!
wait "$probe_pid"
