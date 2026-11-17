"""Publish complete files by rename; cross-device copies are never visible halfway."""
import errno
import os
from pathlib import Path
import shutil
import uuid


def move_output(source, target):
    source, target = Path(source), Path(target)
    try:
        source.replace(target)
        return
    except OSError as error:
        if error.errno != errno.EXDEV:
            raise
    temporary = target.with_name('.' + target.name + '.' + uuid.uuid4().hex + '.partial')
    try:
        shutil.copyfile(source, temporary)
        with temporary.open('r+b') as stream:
            os.fsync(stream.fileno())
        temporary.replace(target)
        source.unlink()
    finally:
        temporary.unlink(missing_ok=True)
