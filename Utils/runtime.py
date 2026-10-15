"""Use optional project-local tools without changing system configuration."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def configure_runtime():
    binary_directory = ROOT / '.tools' / 'ffmpeg' / 'bin'
    if binary_directory.is_dir():
        current = os.environ.get('PATH', '')
        if str(binary_directory) not in current.split(os.pathsep):
            os.environ['PATH'] = str(binary_directory) + os.pathsep + current
    browser_directory = ROOT / '.tools' / 'ms-playwright'
    if browser_directory.is_dir():
        os.environ.setdefault('PLAYWRIGHT_BROWSERS_PATH', str(browser_directory))
